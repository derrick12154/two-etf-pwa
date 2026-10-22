"""Self-contained v3 policy entry; static frozen v2 history adapter; no embedded seed."""
from __future__ import annotations
import argparse
import base64
import io
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import weekly_update as legacy
from sealed_data import unseal, write_sealed
from strategy import allocate, metrics
from volatility_research import simulate, audit, volatility_panel, risk_weight, rebalance_gate
V2_EFFECTIVE_THURSDAY = pd.Timestamp('2026-10-22')
V2_STRATEGY_ID = 'two-etf-thu-fri-v2'
V2_ENGINE_VERSION = 'thu-fri-2026-10-20.2'

def v2_iso(date):
    return str(pd.Timestamp(date).date())

def v2_safe_quote_day(now):
    """Friday can confirm after 15:30; other days retain the source's 18:00 guard."""
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    cutoff = (15, 30) if local.weekday() == 4 else (18, 0)
    return pd.Timestamp(local.date()) - pd.Timedelta(days=0 if (local.hour, local.minute) >= cutoff else 1)

def v2_snapshot_is_current(previous, as_of, end):
    """Reuse a verified phase, so successful Friday data is not rewritten on weekends."""
    if previous.get('engineVersion') != V2_ENGINE_VERSION or previous.get('priceAsOf') != v2_iso(end):
        return False
    as_of = pd.Timestamp(as_of)
    thursday = as_of - pd.Timedelta(days=(as_of.weekday() - 3) % 7)
    if thursday >= V2_EFFECTIVE_THURSDAY and pd.Timestamp(previous.get('calculationAsOf', '1900-01-01')) < thursday:
        return False
    latest = previous.get('latestSignal', {})
    if latest.get('status', '').startswith('pending_') and pd.Timestamp(latest['executionDate']) <= end:
        return False
    return bool(previous.get('provenance', {}).get('portfolioAudit', {}).get('passed'))

def v2_signal_sessions(calendar, through, first=None):
    """One Mon-Thu observation per elapsed Thursday; never use a Friday bar."""
    through = pd.Timestamp(through)
    rows = calendar[(calendar.weekday <= 3) & (calendar <= through)]
    if first is not None:
        rows = rows[rows >= pd.Timestamp(first)]
    periods = rows.to_period('W-FRI')
    elapsed = periods.end_time.normalize() - pd.Timedelta(days=1) <= through
    rows = rows[elapsed]
    return pd.DatetimeIndex(pd.Series(rows, index=rows).groupby(rows.to_period('W-FRI')).last())

def v2_thursday_indicators(raw_close, calendar, through, cfg):
    dates = v2_signal_sessions(calendar, through, raw_close.index[0])
    closes = raw_close.reindex(dates)
    momentum = legacy.base.momentum_panel(closes, cfg['momentum_window'])
    ma = pd.DataFrame({s: closes[s].dropna().rolling(cfg['trend_ma_weeks'], min_periods=cfg['trend_ma_weeks']).mean().reindex(dates) for s in closes}, index=dates)
    return (closes, momentum, ma)

def v2_candidates_at(closes, momentum, ma, assets, vol, date, cfg):
    result = []
    for symbol in closes.columns:
        (m, price, average) = (float(momentum.loc[date, symbol]), float(closes.loc[date, symbol]), float(ma.loc[date, symbol]))
        (scale, sigma) = (float(assets.loc[date, symbol]), float(vol.loc[date, symbol]))
        if not all(np.isfinite([m, price, average, scale, sigma])) or average <= 0:
            raise ValueError('Signal inputs contain missing warmup, price or scale data')
        result.append(dict(symbol=symbol, name=legacy.NAMES[symbol], code=symbol[2:], momentum=m, maDistance=price / average - 1, assetsYi=scale, volatility=sigma, eligible=bool(m > 0 and price > average and (scale > cfg['minimum_net_assets_yi'])), theoreticalWeight=risk_weight(sigma, cfg['target_vol'])))
    return sorted(result, key=lambda row: -row['momentum'])

def v2_account_at(cash, units, prices):
    values = units * prices
    equity = float(cash + values.sum())
    active = units > 1e-07
    if active.sum() > 1 or equity <= 0 or cash < -1e-05:
        raise ValueError('Invalid single-ETF account')
    held = units.index[active][0] if active.any() else 'CASH'
    return (held, float(values.sum() / equity), equity, values)

def v2_make_plan(panel, calendar, decision, cfg, cash, units, vol):
    decision = pd.Timestamp(decision)
    if decision.weekday() != 3:
        raise ValueError('Decisions must be made after Thursday close')
    friday = decision + pd.Timedelta(days=1)
    monday = decision - pd.Timedelta(days=3)
    available = calendar[(calendar >= monday) & (calendar <= decision)]
    if not len(available) or friday not in calendar:
        return (dict(decisionDate=v2_iso(decision), signalDate=v2_iso(available[-1]) if len(available) else None, executionDate=v2_iso(friday), executionTiming='friday_close_proxy', status='skipped_holiday', skipReason='friday_closed' if friday not in calendar else 'no_pre_friday_session', target=None, plannedWeight=None, traded=False, selection=False, auditPassed=True), None)
    signal = available[-1]
    (closes, momentum, ma) = v2_thursday_indicators(panel['raw_close'].loc[:signal], calendar, decision, cfg)
    candidates = v2_candidates_at(closes, momentum, ma, panel['assets'], vol, signal, cfg)
    (held, weight, _, _) = v2_account_at(cash, units, panel['close'].loc[signal])
    count = len(calendar[(calendar >= pd.Timestamp(cfg['backtest_start'])) & (calendar <= decision)].to_period('W-FRI').unique())
    selection = count % cfg['rebalance_every_weeks'] == 0
    eligible = [c['symbol'] for c in candidates if c['eligible']]
    selected = (eligible[0] if eligible else 'CASH') if selection else held if held in eligible else 'CASH'
    sigma = float(vol.loc[signal, selected]) if selected != 'CASH' else np.nan
    target = risk_weight(sigma, cfg['target_vol']) if selected != 'CASH' else 0.0
    before = weight if held == selected and selected != 'CASH' else 0.0
    (trade, reason) = rebalance_gate(before, target, sigma, cfg['target_vol'], cfg['primary_band'], cfg['risk_buffer'], held != selected)
    return (dict(decisionDate=v2_iso(decision), signalDate=v2_iso(signal), executionDate=v2_iso(friday), executionTiming='friday_close_proxy', asset=selected, previousAsset=held, selection=selection, reason='scheduled_selection' if selection else 'eligibility_exit' if held != 'CASH' and selected == 'CASH' else 'weekly_hold', traded=bool(trade), target=float(target), plannedWeight=float(target if trade else before), signalWeight=before, previousWeight=weight, sizingReason=reason, status='pending_friday_close', auditPassed=True), candidates)

def v2_execute_plan(plan, panel, date, cfg, cash, units):
    """Execute the frozen Thursday plan at Friday close; Friday cannot reselect."""
    date = pd.Timestamp(date)
    if date.weekday() != 4 or v2_iso(date) != plan['executionDate'] or pd.Timestamp(plan['signalDate']) >= date:
        raise ValueError('Invalid Friday execution date')
    prices = panel['close'].loc[date]
    (_, _, equity, before) = v2_account_at(cash, units, prices)
    (fills, cost) = ([], 0.0)
    if plan['traded']:
        needed = (units > 1e-12) | (units.index == plan['asset']) & (plan['target'] > 0)
        if panel['raw_close'].loc[date, needed].isna().any() or not panel['tradable'].loc[date, needed].all():
            raise ValueError('Friday close cannot confirm tradable quotes; publication aborted')
        weights = np.zeros(len(units))
        if plan['asset'] != 'CASH':
            weights[units.index.get_loc(plan['asset'])] = plan['target']
        (after, cash, cost) = allocate(equity, before.to_numpy(), weights, (cfg['commission_bps'] + cfg['slippage_bps']) / 10000)
        changes = after - before.to_numpy()
        for col in np.flatnonzero(abs(changes) > 1e-06):
            fills.append(dict(date=date, signal_date=pd.Timestamp(plan['signalDate']), symbol=units.index[col], side='BUY' if changes[col] > 0 else 'SELL', adjusted_price=float(prices.iloc[col]), units_delta=float(changes[col] / prices.iloc[col]), notional=float(abs(changes[col])), cost=float(abs(changes[col]) * (cfg['commission_bps'] + cfg['slippage_bps']) / 10000), phase='close'))
        units = pd.Series(after / prices.to_numpy(), index=units.index)
    (held, weight, _, _) = v2_account_at(cash, units, prices)
    result = dict(plan, status='model_executed' if plan['traded'] else 'model_held', actualHolding=held, executionWeight=weight, executionPriceBasis='adjusted_daily_close_proxy')
    return (result, cash, units, fills, cost)

def v2_legacy_decisions(review, sizing):
    records = []
    for (_, row) in review.iterrows():
        size = sizing.loc[sizing.date.eq(row.date)].iloc[0]
        records.append(dict(signalDate=v2_iso(row.weekly_signal_date), executionDate=v2_iso(row.date), executionTiming='legacy_next_open', asset=row.actual_holding, actualHolding=row.actual_holding, previousAsset=row.previous_holding, selection=bool(row.is_selection_day), reason=row.decision_reason, sizingReason=size.decision_reason, traded=bool(size.should_rebalance), target=float(size.target_weight), plannedWeight=float(size.target_weight if size.should_rebalance else size.signal_weight), executionWeight=float(size.actual_open_weight), status='model_executed' if size.should_rebalance else 'model_held', auditPassed=True))
    return records

def v2_audit_combined(panel, cfg, daily, fills, new_plans, calendar):
    """Independently rebuild cash/units and verify all new decision inputs."""
    cash = float(cfg['initial_capital'])
    units = pd.Series(0.0, index=panel['close'].columns)
    rate = (cfg['commission_bps'] + cfg['slippage_bps']) / 10000
    by_date = {date: rows for (date, rows) in fills.groupby('date')}
    (states, errors) = ({}, [])
    for (date, row) in daily.iterrows():
        for fill in by_date.get(date, pd.DataFrame()).itertuples():
            assert fill.signal_date < fill.date
            assert np.isclose(fill.adjusted_price, panel['close' if fill.phase == 'close' else 'open'].loc[date, fill.symbol])
            assert np.isclose(abs(fill.units_delta) * fill.adjusted_price, fill.notional)
            assert np.isclose(fill.cost, rate * fill.notional)
            cash -= (1 if fill.side == 'BUY' else -1) * fill.notional + fill.cost
            units[fill.symbol] += fill.units_delta
        (held, exposure, equity, _) = v2_account_at(cash, units, panel['close'].loc[date])
        assert held == row.holding and abs(cash - row.cash) < 0.0001
        assert np.isclose(exposure, row.exposure, atol=1e-10)
        errors.append(abs(equity - row.equity))
        states[date] = (cash, units.copy())
    assert max(errors) < 0.0001 and np.isclose(daily.cost.sum(), fills.cost.sum(), atol=1e-05)
    for plan in new_plans:
        (decision, execution) = (pd.Timestamp(plan['decisionDate']), pd.Timestamp(plan['executionDate']))
        assert decision.weekday() == 3 and execution == decision + pd.Timedelta(days=1)
        sessions = calendar[(calendar >= decision - pd.Timedelta(days=3)) & (calendar <= decision)]
        if plan['status'] == 'skipped_holiday':
            assert execution not in calendar or not len(sessions)
            assert not any(fills.loc[fills.phase.eq('close'), 'date'].eq(execution))
            continue
        signal = pd.Timestamp(plan['signalDate'])
        assert signal == sessions[-1] and signal < execution
        observation_dates = v2_signal_sessions(calendar, decision, panel['raw_close'].index[0])
        ranked = []
        for symbol in panel['raw_close']:
            observed = panel['raw_close'][symbol].reindex(observation_dates).dropna()
            momentum = float(observed.iloc[-1] / observed.iloc[-cfg['momentum_window'] - 1] - 1)
            average = observed.tail(cfg['trend_ma_weeks']).mean()
            if momentum > 0 and observed.iloc[-1] > average and (panel['assets'].loc[signal, symbol] > cfg['minimum_net_assets_yi']):
                ranked.append((symbol, momentum))
        ranked.sort(key=lambda item: -item[1])
        (prior_cash, prior_units) = states[signal]
        (held, weight, _, _) = v2_account_at(prior_cash, prior_units, panel['close'].loc[signal])
        selection = len(calendar[(calendar >= pd.Timestamp(cfg['backtest_start'])) & (calendar <= decision)].to_period('W-FRI').unique()) % cfg['rebalance_every_weeks'] == 0
        eligible = [row[0] for row in ranked]
        expected = (eligible[0] if eligible else 'CASH') if selection else held if held in eligible else 'CASH'
        assert plan['selection'] == selection and plan['asset'] == expected
        if expected == 'CASH':
            (target, sigma, before) = (0.0, np.nan, 0.0)
        else:
            returns = panel['raw_close'].loc[:signal, expected].dropna().pct_change(fill_method=None).dropna()
            sigma = returns.tail(cfg['volatility_window']).std(ddof=1) * np.sqrt(252)
            target = 1.0 if sigma == 0 else min(1.0, cfg['target_vol'] / sigma)
            before = weight if expected == held else 0.0
        mandatory = held != expected
        risk_excess = before > target and (not np.isfinite(sigma) or before * sigma > cfg['target_vol'] * (1 + cfg['risk_buffer']) + 1e-12)
        trade = mandatory or risk_excess or abs(before - target) >= cfg['primary_band'] - 1e-12
        assert np.isclose(plan['target'], target) and plan['traded'] == trade
        assert np.isclose(plan['plannedWeight'], target if trade else before)
        if plan['status'] != 'pending_friday_close':
            assert execution in daily.index
            assert np.isclose(plan['executionWeight'], daily.loc[execution, 'exposure'])
            if trade:
                assert np.isclose(plan['executionWeight'], target, atol=1e-09)
    return dict(passed=True, max_equity_error_cny=max(errors), fills=len(fills), new_decisions=len(new_plans), causalThursdayInputs=True, fridayCloseProxy=True)

def v2_build_record(seed, directory, calendar, as_of):
    as_of = pd.Timestamp(as_of)
    end = calendar[calendar <= as_of][-1]
    if calendar[-1] < as_of + pd.Timedelta(days=10):
        raise ValueError('Exchange calendar does not cover the upcoming decision and execution dates')
    cfg = dict(seed['config'], symbols=legacy.SYMBOLS, weekly_exit_on_ineligible=True, last_week_complete=False)
    legacy.base.DATA = directory
    (panel, _, manifest, _, _) = legacy.base.load_market(cfg, v2_iso(end))
    cutoff = min(end, V2_EFFECTIVE_THURSDAY)
    (old_panel, _, _, _, _) = legacy.base.load_market(cfg, v2_iso(cutoff))
    start = int(old_panel['close'].index.searchsorted(pd.Timestamp(cfg['backtest_start'])))
    (full, old_fills, review) = legacy.base.simulate_weekly(old_panel, cfg, start, allowed=legacy.SYMBOLS)
    selection_audit = legacy.base.audit(old_panel, cfg, full, old_fills, review, allowed=legacy.SYMBOLS)
    old_vol = volatility_panel(old_panel['raw_close'], cfg['volatility_window'])
    options = dict(target=cfg['target_vol'], band=cfg['primary_band'], risk_buffer=cfg['risk_buffer'])
    (daily, fills, sizing) = simulate(old_panel, cfg, start, review, old_vol, **options)
    audit(old_panel, cfg, start, review, daily, fills, sizing, cfg['volatility_window'], **options)
    baseline = pd.read_csv(io.StringIO(seed['acceptedDaily']), parse_dates=['date']).set_index('date')
    np.testing.assert_allclose(daily.loc[baseline.index, 'nav'], baseline.nav, rtol=1e-10, atol=1e-12)
    np.testing.assert_allclose(daily.loc[baseline.index, 'exposure'], baseline.exposure, rtol=1e-10, atol=1e-12)
    assert daily.loc[baseline.index, 'holding'].equals(baseline.holding)
    fills = fills.assign(phase='open')
    decisions = v2_legacy_decisions(review, sizing)
    latest = dict(decisions[-1])
    confirmed = dict(latest)
    signal_date = pd.Timestamp(latest['signalDate'])
    candidates = v2_candidates_at(old_panel['weekly_close'], old_panel['weekly_momentum'], old_panel['weekly_ma'], old_panel['assets'], old_vol, signal_date, cfg)
    cash = float(daily.cash.iloc[-1])
    units = pd.Series(0.0, index=panel['close'].columns)
    if daily.holding.iloc[-1] != 'CASH':
        held = daily.holding.iloc[-1]
        units[held] = (daily.equity.iloc[-1] - cash) / panel['close'].loc[daily.index[-1], held]
    previous = float(daily.equity.iloc[-1])
    (new_rows, new_fills, plans) = ([], [], [])
    vol = volatility_panel(panel['raw_close'], cfg['volatility_window'])
    for date in pd.date_range(V2_EFFECTIVE_THURSDAY, as_of):
        (fee, turnover) = (0.0, 0.0)
        if date.weekday() == 3:
            (latest, current_candidates) = v2_make_plan(panel, calendar, date, cfg, cash, units, vol)
            plans.append(latest)
            if current_candidates is not None:
                (candidates, signal_date) = (current_candidates, pd.Timestamp(latest['signalDate']))
        if date.weekday() == 4 and latest.get('status') == 'pending_friday_close' and (v2_iso(date) == latest['executionDate']) and (date <= end):
            (latest, cash, units, executed_fills, fee) = v2_execute_plan(latest, panel, date, cfg, cash, units)
            new_fills.extend(executed_fills)
            turnover = sum((fill['notional'] for fill in executed_fills))
            plans[-1] = latest
            decisions.append(latest)
            confirmed = dict(latest)
        if date in panel['close'].index and date > daily.index[-1] and (date <= end):
            (held, exposure, equity, _) = v2_account_at(cash, units, panel['close'].loc[date])
            new_rows.append(dict(date=date, equity=equity, nav=equity / cfg['initial_capital'], **{'return': equity / previous - 1}, cash=cash, cash_weight=cash / equity, exposure=exposure, cost=fee, turnover=turnover / (equity + fee), holding=held, held_price_missing=False))
            previous = equity
    if new_rows:
        daily = pd.concat([daily, pd.DataFrame(new_rows).set_index('date')])
    if new_fills:
        fills = pd.concat([fills, pd.DataFrame(new_fills)], ignore_index=True)
    portfolio_audit = v2_audit_combined(panel, cfg, daily, fills, plans, calendar)
    (old_end, old_execution) = legacy.complete_week(calendar, as_of)
    if as_of < V2_EFFECTIVE_THURSDAY and old_execution > end and (old_execution < V2_EFFECTIVE_THURSDAY):
        old_cfg = dict(cfg, last_week_complete=True)
        (pending_panel, _, _, _, _) = legacy.base.load_market(old_cfg, v2_iso(old_end))
        (latest, candidates) = legacy.next_signal(pending_panel, old_cfg, daily.loc[:old_end], vol, old_execution)
        latest['executionTiming'] = 'legacy_next_open'
        signal_date = old_end
    (history, peak) = ([], 1.0)
    for (date, row) in daily.iterrows():
        peak = max(peak, float(row.nav))
        entry = dict(date=v2_iso(date), nav=float(row.nav), exposure=float(row.exposure), drawdown=float(row.nav / peak - 1))
        for symbol in legacy.SYMBOLS:
            entry[symbol] = float(panel['close'].loc[date, symbol] / panel['close'].iloc[start - 1][symbol])
        history.append(entry)
    next_thursday = as_of + pd.Timedelta(days=(3 - as_of.weekday()) % 7 or 7)
    next_thursday = max(next_thursday, V2_EFFECTIVE_THURSDAY)
    next_friday = next_thursday + pd.Timedelta(days=1)
    next_refresh = as_of + pd.Timedelta(days=1) if as_of.weekday() == 3 and latest['status'] == 'pending_friday_close' else next_thursday
    return dict(schemaVersion=2, strategyId=V2_STRATEGY_ID, engineVersion=V2_ENGINE_VERSION, calculationAsOf=v2_iso(as_of), title='双市场趋势', mode='automated_thursday_friday', generatedAt=datetime.now(ZoneInfo('UTC')).isoformat(), priceAsOf=v2_iso(end), signalAsOf=v2_iso(signal_date), positionAsOf=confirmed['executionDate'], positionTiming=confirmed['executionTiming'], executionDate=confirmed['executionDate'], holding=confirmed['actualHolding'], exposure=float(confirmed['executionWeight']), latestSignal=latest, pendingSignal=latest if latest['status'].startswith('pending_') else None, lastTarget=float(confirmed['target']), lastSizingReason=confirmed['sizingReason'], nav=float(daily.nav.iloc[-1]), summary=metrics(daily, 252), annual=legacy.annual_drawdown(daily), candidates=candidates, history=history, decisions=list(reversed(decisions[-52:])), nextSignalDate=v2_iso(next_thursday), nextExecutionDate=v2_iso(next_friday), nextRefreshExpected=v2_iso(next_refresh), timing=dict(effectiveDecisionDate=v2_iso(V2_EFFECTIVE_THURSDAY), firstExecutionDate=v2_iso(V2_EFFECTIVE_THURSDAY + pd.Timedelta(days=1)), decision='thursday_after_close', execution='friday_tail', modelFill='friday_daily_close_proxy', positionRefresh='after_friday_execution', holidayPolicy='skip_week_when_friday_closed_or_no_mon_thu_quote'), rules=dict(momentumWeeks=26, maWeeks=40, selectionWeeks=4, targetVol=0.2, volatilityDays=63, band=0.05, riskLimit=0.22, minAssetsYi=10), provenance=dict(priceHashes=manifest['hashes'], selectionAudit=selection_audit, portfolioAudit=portfolio_audit, acceptedBaselineIdentical=True, acceptedBaselineThrough=seed['anchor'], legacyTimingThrough=v2_iso(cutoff), calendarHash=manifest['calendarSha256'], priceCrosscheck=manifest['priceCrosscheck'], dataSources=[legacy.source.SINA_URL, legacy.source.SINA_FACTOR_URL, legacy.source.SCALE_URL, legacy.CALENDAR_URL, 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get']))
from types import SimpleNamespace
old = SimpleNamespace(legacy=legacy, simulate=simulate, audit=audit, volatility_panel=volatility_panel, risk_weight=risk_weight, rebalance_gate=rebalance_gate, iso=v2_iso, safe_quote_day=v2_safe_quote_day, snapshot_is_current=v2_snapshot_is_current, signal_sessions=v2_signal_sessions, thursday_indicators=v2_thursday_indicators, candidates_at=v2_candidates_at, account_at=v2_account_at, make_plan=v2_make_plan, execute_plan=v2_execute_plan, legacy_decisions=v2_legacy_decisions, audit_combined=v2_audit_combined, build_record=v2_build_record, EFFECTIVE_THURSDAY=V2_EFFECTIVE_THURSDAY, ENGINE_VERSION=V2_ENGINE_VERSION, STRATEGY_ID=V2_STRATEGY_ID)
import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
from sealed_data import unseal, write_sealed
from strategy import metrics
EFFECTIVE_THURSDAY = pd.Timestamp('2026-10-29')
ENGINE_VERSION = 'risk-blend-2026-10-23.1'
STRATEGY_ID = 'two-etf-risk-blend-v3'
SYMBOLS = ['sz159915', 'sh513100']
for _name in ('legacy', 'simulate', 'audit', 'volatility_panel', 'risk_weight', 'rebalance_gate', 'iso', 'safe_quote_day', 'signal_sessions', 'thursday_indicators', 'account_at', 'make_plan', 'execute_plan'):
    globals()[_name] = getattr(old, _name)

def policy():
    return dict(id=STRATEGY_ID, effectiveDecisionDate=iso(EFFECTIVE_THURSDAY), firstExecutionDate=iso(EFFECTIVE_THURSDAY + pd.Timedelta(days=1)), selectionRanking='momentum26_over_volatility63', sizeGateEnabled=False, sleeveCount=4, phaseAnchor='2020-01-06', initialSplit='equal_cash_and_units', restoreEqualWeights=False, portfolioReleverage=False, netTradeFees=False)

def candidate_scores(panel, calendar, decision, cfg, vol):
    available = calendar[(calendar >= decision - pd.Timedelta(days=3)) & (calendar <= decision)]
    signal = available[-1]
    (closes, momentum, ma) = thursday_indicators(panel['raw_close'].loc[:signal], calendar, decision, cfg)
    rows = []
    for symbol in panel['raw_close']:
        (price, m, average, sigma) = map(float, [closes.loc[signal, symbol], momentum.loc[signal, symbol], ma.loc[signal, symbol], vol.loc[signal, symbol]])
        if not np.isfinite([price, m, average, sigma]).all() or average <= 0 or sigma < 0:
            raise ValueError('Missing or invalid risk-ranking observations; retaining verified snapshot')
        eligible = bool(m > 0 and price > average)
        if eligible and sigma <= 0:
            raise ValueError('Eligible asset needs positive volatility for risk ranking')
        rows.append(dict(symbol=symbol, name=legacy.NAMES[symbol], code=symbol[2:], momentum=m, maDistance=price / average - 1, volatility=sigma, riskScore=m / sigma if sigma > 0 else None, eligible=eligible, theoreticalWeight=risk_weight(sigma, 0.2)))
    return sorted(rows, key=lambda c: (-(c['riskScore'] if c['riskScore'] is not None else -np.inf), -c['momentum']))

def sleeve_plan(panel, calendar, decision, cfg, cash, units, phase, vol, candidates=None):
    decision = pd.Timestamp(decision)
    friday = decision + pd.Timedelta(days=1)
    if decision.weekday() != 3:
        raise ValueError('Thursday decision required')
    available = calendar[(calendar >= decision - pd.Timedelta(days=3)) & (calendar <= decision)]
    common = dict(decisionDate=iso(decision), signalDate=iso(available[-1]) if len(available) else None, executionDate=iso(friday), executionTiming='friday_close_proxy', phase=phase, rankingMethod='momentum26_over_volatility63', auditPassed=True)
    if not len(available) or friday not in calendar:
        return dict(common, status='skipped_holiday', skipReason='friday_closed' if friday not in calendar else 'no_pre_friday_session', target=None, plannedWeight=None, traded=False, selection=False)
    signal = available[-1]
    candidates = candidate_scores(panel, calendar, decision, cfg, vol) if candidates is None else candidates
    eligible = [c['symbol'] for c in candidates if c['eligible']]
    count = len(calendar[(calendar >= pd.Timestamp(cfg['backtest_start'])) & (calendar <= decision)].to_period('W-FRI').unique())
    selection = count % 4 == phase
    (held, weight, equity, _) = account_at(cash, units, panel['close'].loc[signal])
    asset = (eligible[0] if eligible else 'CASH') if selection else held if held in eligible else 'CASH'
    sigma = float(vol.loc[signal, asset]) if asset != 'CASH' else np.nan
    target = risk_weight(sigma, 0.2) if asset != 'CASH' else 0.0
    before = weight if asset == held and asset != 'CASH' else 0.0
    (traded, reason) = rebalance_gate(before, target, sigma, 0.2, 0.05, 0.1, held != asset)
    return dict(common, status='pending_friday_close', asset=asset, previousAsset=held, selection=bool(selection), observedWeekCount=count, target=float(target), plannedWeight=float(target if traded else before), traded=bool(traded), sizingReason=reason, signalWeight=float(before), previousWeight=float(weight), signalEquity=float(equity), reason='scheduled_selection' if selection else 'eligibility_exit' if held != 'CASH' and asset == 'CASH' else 'weekly_hold')

def allocations(cash, units, prices):
    values = units * prices
    equity = float(cash + values.sum())
    if equity <= 0 or cash < -1e-05 or units.lt(-1e-07).any():
        raise ValueError('Invalid aggregate account')
    return {s: float(max(0.0, values[s] / equity)) for s in SYMBOLS} | {'CASH': float(max(0.0, cash / equity))}

def label(weights):
    active = [s for s in SYMBOLS if weights[s] > 1e-10]
    return 'MIXED' if len(active) > 1 else active[0] if active else 'CASH'

def aggregate_plan(plans):
    if all((p['status'] == 'skipped_holiday' for p in plans)):
        return dict(plans[0], phase=None, sleeves=plans)
    total = sum((p['signalEquity'] for p in plans))
    planned = {s: 0.0 for s in SYMBOLS + ['CASH']}
    theory = planned.copy()
    for p in plans:
        fraction = p['signalEquity'] / total
        for (weights, key) in ((planned, 'plannedWeight'), (theory, 'target')):
            if p['asset'] != 'CASH':
                weights[p['asset']] += fraction * p[key]
            weights['CASH'] += fraction * (1 - p[key])
    return dict(decisionDate=plans[0]['decisionDate'], signalDate=plans[0]['signalDate'], executionDate=plans[0]['executionDate'], executionTiming='friday_close_proxy', status='pending_friday_close', asset=label(planned), target=1 - theory['CASH'], plannedWeight=1 - planned['CASH'], plannedWeights=planned, theoreticalWeights=theory, traded=any((p['traded'] for p in plans)), selection=any((p['selection'] for p in plans)), selectedPhases=[p['phase'] for p in plans if p['selection']], sizingReason='four_phase', reason='four_phase_review', rankingMethod='momentum26_over_volatility63', sleeves=plans, auditPassed=True)

def simulate_sleeves(panel, calendar, cfg, as_of, initial_cash, initial_units):
    cash = [initial_cash / 4] * 4
    units = [initial_units / 4 for _ in range(4)]
    vol = volatility_panel(panel['raw_close'], 63)
    pending = None
    candidates = []
    daily = []
    fills = []
    plans = []
    states = []
    for date in pd.date_range(EFFECTIVE_THURSDAY, as_of):
        fee = notional = 0.0
        if date.weekday() == 3:
            available = calendar[(calendar >= date - pd.Timedelta(days=3)) & (calendar <= date)]
            valid = len(available) and date + pd.Timedelta(days=1) in calendar
            if valid:
                candidates = candidate_scores(panel, calendar, date, cfg, vol)
            pending = aggregate_plan([sleeve_plan(panel, calendar, date, cfg, cash[i], units[i], i, vol, candidates if valid else None) for i in range(4)])
            plans.append(pending)
        if date.weekday() == 4 and pending is not None and (pending['status'] == 'pending_friday_close') and (date in panel['close'].index):
            confirmed = []
            for (i, p) in enumerate(pending['sleeves']):
                (done, cash[i], units[i], trades, cost) = execute_plan(p, panel, date, cfg, cash[i], units[i])
                fee += cost
                notional += sum((t['notional'] for t in trades))
                for t in trades:
                    t['sleevePhase'] = i
                fills.extend(trades)
                confirmed.append(done)
            total_cash = sum(cash)
            total_units = sum(units)
            weights = allocations(total_cash, total_units, panel['close'].loc[date])
            pending = dict(pending, status='model_executed' if pending['traded'] else 'model_held', sleeves=confirmed, actualHolding=label(weights), executionWeight=1 - weights['CASH'], executionWeights=weights, executionPriceBasis='adjusted_daily_close_proxy')
            plans[-1] = pending
        if date in panel['close'].index:
            total_cash = sum(cash)
            total_units = sum(units)
            equity = total_cash + (total_units * panel['close'].loc[date]).sum()
            weights = allocations(total_cash, total_units, panel['close'].loc[date])
            daily.append(dict(date=date, equity=float(equity), cash=float(total_cash), exposure=1 - weights['CASH'], cost=fee, turnover=notional / (equity + fee), weights=weights))
            states.append([dict(phase=i, cash=float(cash[i]), units={s: float(units[i][s]) for s in SYMBOLS}) for i in range(4)])
    return (daily, fills, plans, candidates, states)

def audit_sleeves(panel, calendar, cfg, daily, fills, plans, initial_cash, initial_units, states):
    """Independent prefix signals, analytic allocations, gross trade and cash ledger."""
    cash = [initial_cash / 4] * 4
    units = [initial_units / 4 for _ in range(4)]
    ledger_cash = cash.copy()
    ledger_units = [u.copy() for u in units]
    by_decision = {pd.Timestamp(p['decisionDate']): p for p in plans}
    by_execution = {pd.Timestamp(p['executionDate']): p for p in plans}
    by_date = {pd.Timestamp(row['date']): (row, states[i]) for (i, row) in enumerate(daily)}
    rate = (cfg['commission_bps'] + cfg['slippage_bps']) / 10000
    maximum = 0.0
    decisions = 0
    for date in pd.date_range(EFFECTIVE_THURSDAY, pd.Timestamp(daily[-1]['date'])):
        expected_fee = 0.0
        if date in by_decision:
            plan = by_decision[date]
            available = calendar[(calendar >= date - pd.Timedelta(days=3)) & (calendar <= date)]
            if plan['status'] == 'skipped_holiday':
                assert not len(available) or date + pd.Timedelta(days=1) not in calendar
            else:
                signal = available[-1]
                observed = signal_sessions(calendar, date, panel['raw_close'].index[0])
                ranked = []
                sigmas = {}
                for symbol in panel['raw_close']:
                    samples = panel['raw_close'][symbol].reindex(observed).dropna()
                    m = float(samples.iloc[-1] / samples.iloc[-27] - 1)
                    ma = float(samples.tail(40).mean())
                    ret = panel['raw_close'].loc[:signal, symbol].dropna().pct_change(fill_method=None).dropna()
                    sigma = float(ret.tail(63).std(ddof=1) * np.sqrt(252))
                    sigmas[symbol] = sigma
                    assert np.isfinite([samples.iloc[-1], m, ma, sigma]).all()
                    if m > 0 and samples.iloc[-1] > ma:
                        assert sigma > 0
                        ranked.append((symbol, m / sigma, m))
                ranked.sort(key=lambda row: (-row[1], -row[2]))
                eligible = [row[0] for row in ranked]
                count = len(calendar[(calendar >= pd.Timestamp(cfg['backtest_start'])) & (calendar <= date)].to_period('W-FRI').unique())
                for (i, p) in enumerate(plan['sleeves']):
                    values = units[i] * panel['close'].loc[signal]
                    equity = cash[i] + values.sum()
                    held = units[i].index[units[i].gt(1e-07)][0] if units[i].gt(1e-07).any() else 'CASH'
                    selected = (eligible[0] if eligible else 'CASH') if count % 4 == i else held if held in eligible else 'CASH'
                    target = min(1.0, 0.2 / sigmas[selected]) if selected != 'CASH' else 0.0
                    before = float(values.sum() / equity) if held == selected and selected != 'CASH' else 0.0
                    risk = selected != 'CASH' and before > target and (before * sigmas[selected] > 0.22 + 1e-12)
                    trade = held != selected or risk or abs(before - target) >= 0.05 - 1e-12
                    assert p['phase'] == i and p['asset'] == selected and (p['selection'] == (count % 4 == i))
                    assert p['traded'] == trade and np.isclose(p['target'], target, atol=1e-11)
                    assert np.isclose(p['plannedWeight'], target if trade else before, atol=1e-11)
                    assert np.isclose(p['signalEquity'], equity, atol=1e-05)
                    decisions += 1
        if date in by_execution and by_execution[date]['status'] in ('model_executed', 'model_held'):
            for (i, p) in enumerate(by_execution[date]['sleeves']):
                if not p['traded']:
                    continue
                prices = panel['close'].loc[date]
                values = units[i] * prices
                equity = cash[i] + values.sum()
                old_value = float(values.sum())
                w = p['target']
                held = units[i].index[units[i].gt(1e-07)][0] if units[i].gt(1e-07).any() else 'CASH'
                if held == p['asset'] and held != 'CASH':
                    post = (equity + rate * old_value) / (1 + rate * w) if w * equity >= old_value else (equity - rate * old_value) / (1 - rate * w)
                else:
                    post = (equity - rate * old_value) / (1 + rate * w)
                target_values = pd.Series(0.0, index=SYMBOLS)
                if p['asset'] != 'CASH':
                    target_values[p['asset']] = post * w
                fee = float((target_values - values).abs().sum() * rate)
                cash[i] = float(equity - fee - target_values.sum())
                units[i] = target_values / prices
                expected_fee += fee
        for fill in [f for f in fills if pd.Timestamp(f['date']) == date]:
            i = fill['sleevePhase']
            symbol = fill['symbol']
            assert date.weekday() == 4 and pd.Timestamp(fill['signal_date']) < date
            assert panel['tradable'].loc[date, symbol] and np.isclose(fill['adjusted_price'], panel['close'].loc[date, symbol])
            assert np.isclose(fill['notional'], abs(fill['units_delta']) * fill['adjusted_price']) and np.isclose(fill['cost'], fill['notional'] * rate)
            ledger_cash[i] -= (1 if fill['side'] == 'BUY' else -1) * fill['notional'] + fill['cost']
            ledger_units[i][symbol] += fill['units_delta']
        if date in by_date:
            (row, saved) = by_date[date]
            prices = panel['close'].loc[date]
            for i in range(4):
                maximum = max(maximum, abs(cash[i] - ledger_cash[i]), abs(cash[i] - saved[i]['cash']))
                np.testing.assert_allclose(units[i], ledger_units[i], atol=1e-06, rtol=1e-10)
                np.testing.assert_allclose(units[i], pd.Series(saved[i]['units']).reindex(SYMBOLS), atol=1e-06, rtol=1e-10)
                assert cash[i] >= -1e-05 and units[i].ge(-1e-07).all() and (units[i].gt(1e-07).sum() <= 1)
            total_cash = sum(cash)
            total_units = sum(units)
            equity = total_cash + (total_units * prices).sum()
            maximum = max(maximum, abs(equity - row['equity']), abs(total_cash - row['cash']))
            assert np.isclose(row['cost'], expected_fee, atol=1e-06)
            assert np.isclose(row['exposure'], float((total_units * prices).sum() / equity), atol=1e-10)
    assert maximum < 0.0001 and np.isclose(sum((d['cost'] for d in daily)), sum((f['cost'] for f in fills)))
    return dict(passed=True, max_equity_error_cny=maximum, new_decisions=decisions, fills=len(fills), causalThursdayInputs=True, fridayCloseProxy=True, fourSleeveLedger=True, grossFees=True, noSizeGate=True)

def build_record(seed, directory, calendar, as_of):
    as_of = pd.Timestamp(as_of)
    if as_of < EFFECTIVE_THURSDAY:
        record = old.build_record(seed, directory, calendar, as_of)
        record.update(engineVersion=ENGINE_VERSION, scheduledPolicy=policy())
        return record
    baseline = old.build_record(seed, directory, calendar, EFFECTIVE_THURSDAY - pd.Timedelta(days=1))
    cfg = dict(seed['config'], symbols=SYMBOLS, weekly_exit_on_ineligible=True, last_week_complete=False)
    legacy.base.DATA = directory
    end = calendar[calendar <= as_of][-1]
    if calendar[-1] < as_of + pd.Timedelta(days=10):
        raise ValueError('Calendar does not cover next execution')
    (panel, _, manifest, _, _) = legacy.base.load_market(cfg, iso(end))
    last = baseline['history'][-1]
    equity = last['nav'] * cfg['initial_capital']
    initial_cash = equity * (1 - last['exposure'])
    initial_units = pd.Series(0.0, index=panel['close'].columns)
    if baseline['holding'] != 'CASH':
        initial_units[baseline['holding']] = (equity - initial_cash) / panel['close'].loc[last['date'], baseline['holding']]
    (daily, fills, plans, candidates, states) = simulate_sleeves(panel, calendar, cfg, as_of, initial_cash, initial_units)
    if not daily:
        raise ValueError('New policy has no confirmed daily observations')
    audit_new = audit_sleeves(panel, calendar, cfg, daily, fills, plans, initial_cash, initial_units, states)
    confirmed = dict(baseline['decisions'][0])
    weights = {s: baseline['exposure'] if baseline['holding'] == s else 0.0 for s in SYMBOLS}
    weights['CASH'] = 1 - baseline['exposure']
    decisions = list(reversed(baseline['decisions']))
    for p in plans:
        if p['status'] in ('model_executed', 'model_held'):
            confirmed = p
            weights = p['executionWeights']
            decisions.append(p)
    history = list(baseline['history'])
    anchor = {s: panel['close'].loc[last['date'], s] / last[s] for s in SYMBOLS}
    peak = max(1.0, max((h['nav'] for h in history)))
    for row in daily:
        nav = row['equity'] / cfg['initial_capital']
        peak = max(peak, nav)
        history.append(dict(date=iso(row['date']), nav=nav, exposure=row['exposure'], drawdown=nav / peak - 1, **{s: float(panel['close'].loc[row['date'], s] / anchor[s]) for s in SYMBOLS}))
    df = pd.DataFrame(history).set_index('date')
    df.index = pd.to_datetime(df.index)
    df['return'] = df.nav.pct_change(fill_method=None).fillna(df.nav.iloc[0] - 1)
    df['turnover'] = 0.0
    df['cost'] = 0.0
    for row in daily:
        df.loc[row['date'], ['turnover', 'cost']] = [row['turnover'], row['cost']]
    summary = metrics(df, 252)
    summary['total_cost'] += baseline['summary']['total_cost']
    summary['annual_traded_notional_over_nav'] += baseline['summary']['annual_traded_notional_over_nav'] * baseline['summary']['days'] / len(df)
    latest = plans[-1]
    next_thursday = as_of + pd.Timedelta(days=(3 - as_of.weekday()) % 7 or 7)
    next_refresh = as_of + pd.Timedelta(days=1) if as_of.weekday() == 3 and latest['status'] == 'pending_friday_close' else next_thursday
    sleeve_states = states[-1]
    current_equities = [s['cash'] + sum((s['units'][symbol] * panel['close'].loc[end, symbol] for symbol in SYMBOLS)) for s in sleeve_states]
    for (i, s) in enumerate(sleeve_states):
        s.update(equity=float(current_equities[i]), equityWeight=float(current_equities[i] / sum(current_equities)))
    return dict(schemaVersion=3, strategyId=STRATEGY_ID, engineVersion=ENGINE_VERSION, mode='automated_four_phase', title='双市场趋势', generatedAt=datetime.now(ZoneInfo('UTC')).isoformat(), calculationAsOf=iso(as_of), priceAsOf=iso(end), signalAsOf=latest.get('signalDate') or baseline['signalAsOf'], candidateAsOf=next((p['signalDate'] for p in reversed(plans) if p['status'] != 'skipped_holiday'), baseline['signalAsOf']), positionAsOf=confirmed['executionDate'], positionTiming=confirmed.get('executionTiming', 'legacy_next_open'), executionDate=confirmed['executionDate'], holding=label(weights), exposure=1 - weights['CASH'], confirmedWeights=weights, latestSignal=latest, pendingSignal=latest if latest['status'].startswith('pending_') else None, lastTarget=confirmed['target'], lastSizingReason=confirmed['sizingReason'], nav=history[-1]['nav'], summary=summary, annual=legacy.annual_drawdown(df), history=history, candidates=candidates or baseline['candidates'], decisions=list(reversed(decisions[-52:])), sleeves=sleeve_states, policy=policy(), nextSignalDate=iso(next_thursday), nextExecutionDate=iso(next_thursday + pd.Timedelta(days=1)), nextRefreshExpected=iso(next_refresh), timing=dict(effectiveDecisionDate=iso(EFFECTIVE_THURSDAY), firstExecutionDate=iso(EFFECTIVE_THURSDAY + pd.Timedelta(days=1)), decision='thursday_after_close', execution='friday_tail', modelFill='friday_daily_close_proxy', positionRefresh='after_friday_execution'), rules=dict(momentumWeeks=26, maWeeks=40, selectionWeeks=4, targetVol=0.2, volatilityDays=63, band=0.05, riskLimit=0.22, minAssetsYi=None, sizeGateEnabled=False, selectionRanking='momentum26_over_volatility63', sleeveCount=4), provenance=dict(priceHashes=manifest['hashes'], selectionAudit=dict(passed=True, independentPrefixDecisions=audit_new['new_decisions']), portfolioAudit=audit_new, acceptedBaselineIdentical=True, acceptedBaselineThrough=seed['anchor'], historicalPolicyThrough=last['date'], transitionEquity=equity, transitionCash=initial_cash, transitionUnits={s: float(initial_units[s]) for s in SYMBOLS}, calendarHash=manifest['calendarSha256'], priceCrosscheck=manifest['priceCrosscheck'], legacyPortfolioAudit=baseline['provenance']['portfolioAudit']))

def snapshot_is_current(previous, as_of, end):
    if previous.get('engineVersion') != ENGINE_VERSION or previous.get('priceAsOf') != iso(end):
        return False
    date = pd.Timestamp(as_of)
    thursday = date - pd.Timedelta(days=(date.weekday() - 3) % 7)
    if thursday >= old.EFFECTIVE_THURSDAY and pd.Timestamp(previous.get('calculationAsOf', '1900-01-01')) < thursday:
        return False
    if date >= EFFECTIVE_THURSDAY and previous.get('schemaVersion') != 3:
        return False
    latest = previous.get('latestSignal', {})
    if latest.get('status', '').startswith('pending_') and pd.Timestamp(latest['executionDate']) <= end:
        return False
    return bool(previous.get('provenance', {}).get('portfolioAudit', {}).get('passed'))

def fetch_snapshot(seed, directory, calendar, end):
    """Fetch prices; archived scale is used only by immutable pre-transition history."""
    legacy.source.DATA = directory
    records = []
    frames = {}
    hashes = {}
    for symbol in SYMBOLS + [legacy.CALENDAR_SYMBOL]:
        meta = legacy.source.fetch_sina_prices({'symbol': symbol}, iso(end))
        frame = pd.read_csv(directory / 'prices' / f'{symbol}.csv', parse_dates=['date']).set_index('date')
        accepted = pd.read_csv(io.StringIO(seed['prices'][symbol]), parse_dates=['date']).set_index('date')
        if not accepted.index.isin(frame.index).all():
            raise ValueError('Source removed accepted historical bars')
        np.testing.assert_allclose(frame.loc[accepted.index, ['open', 'close', 'high', 'low']], accepted[['open', 'close', 'high', 'low']], rtol=1e-08, atol=1e-08)
        frames[symbol] = frame
        hashes[symbol] = meta['sha256']
        if symbol in SYMBOLS:
            records.append(dict(code=symbol[2:], symbol=symbol, **{k: v for (k, v) in meta.items() if k != 'symbol'}))
    legacy.check_recent_prices(frames, calendar, end, seed['anchor'])
    pd.DataFrame(records).to_csv(directory / 'universe.csv', index=False)
    scales = pd.read_csv(io.StringIO(seed['scaleHistory']), parse_dates=['report_date'])
    cutoff = min(pd.Timestamp(end), EFFECTIVE_THURSDAY - pd.Timedelta(days=1))
    required = (cutoff - pd.Timedelta(days=100)).to_period('Q').start_time - pd.Timedelta(days=1)
    for symbol in SYMBOLS:
        if scales.loc[scales.symbol.eq(symbol), 'report_date'].max() < required:
            raise ValueError('Archived scale is insufficient to reproduce pre-transition history')
    scales.to_csv(directory / 'scale_history.csv', index=False, date_format='%Y-%m-%d')
    manifest = dict(hashes=hashes, benchmark_sha256=hashes[legacy.CALENDAR_SYMBOL], calendarSha256=hashlib.sha256('|'.join(map(str, calendar)).encode()).hexdigest(), priceCrosscheck=legacy.crosscheck_prices(frames, end), scalePolicy='archived_for_pretransition_only')
    (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--seed', type=Path, default=Path(__file__).with_name('weekly_seed.enc.json'))
    parser.add_argument('--as-of')
    parser.add_argument('--previous', type=Path, default=Path('data.enc.json'))
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--direct-network', action='store_true')
    args = parser.parse_args()
    password = os.environ.get('APP_PASSWORD', '')
    if not password:
        raise ValueError('APP_PASSWORD must come from Actions secret')
    envelope = json.loads(args.seed.read_text(encoding='utf-8'))
    seed = unseal(envelope, password)
    if args.direct_network:
        legacy.source.session().trust_env = False
    safe = safe_quote_day(datetime.now(ZoneInfo('Asia/Shanghai')))
    as_of = pd.Timestamp(args.as_of) if args.as_of else safe
    if as_of > safe:
        raise ValueError('Cannot use unfinished/future quotes')
    (calendar, _) = legacy.fetch_calendar()
    end = calendar[calendar <= as_of][-1]
    if end < pd.Timestamp(seed['anchor']):
        raise ValueError('Quotes precede baseline')
    if args.previous.exists() and (not args.force):
        previous = unseal(json.loads(args.previous.read_text(encoding='utf-8')), password)
        if snapshot_is_current(previous, as_of, end):
            args.site.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.previous, args.site / 'data.enc.json')
            print('PASS: reuse latest verified policy snapshot')
            return
    workroot = args.site.resolve().parent / '.weekly-work'
    workroot.mkdir(parents=True, exist_ok=True)
    directory = workroot / ('risk-blend-' + uuid.uuid4().hex)
    directory.mkdir()
    try:
        assert directory.resolve().is_relative_to(workroot.resolve())
        fetch_snapshot(seed, directory, calendar, end)
        record = build_record(seed, directory, calendar, as_of)
        args.site.mkdir(parents=True, exist_ok=True)
        write_sealed(args.site / 'data.enc.json', record, password, salt=base64.b64decode(envelope['salt']))
    finally:
        assert directory.resolve().is_relative_to(workroot.resolve())
        shutil.rmtree(directory)
    message = f"PASS: quotes {record['priceAsOf']}; position {record['positionAsOf']}; engine {ENGINE_VERSION}; new policy {iso(EFFECTIVE_THURSDAY)}"
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(message + '\n')
if __name__ == '__main__':
    try:
        main()
    except AssertionError:
        raise SystemExit('Baseline, risk-ranking or four-sleeve account audit failed; no publication.') from None
