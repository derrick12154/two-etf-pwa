"""Thursday-close decisions, Friday-tail execution, and weekly position confirmation.

Daily closes are the explicit model proxy for Friday-tail fills. Existing history
uses the immutable legacy engine through the first new decision date. No broker
orders are sent. APP_PASSWORD remains in the Actions environment only.
"""
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

EFFECTIVE_THURSDAY = pd.Timestamp('2026-10-22')
STRATEGY_ID = 'two-etf-thu-fri-v2'
ENGINE_VERSION = 'thu-fri-2026-10-20.2'


def iso(date):
    return str(pd.Timestamp(date).date())


def safe_quote_day(now):
    """Friday can confirm after 15:30; other days retain the source's 18:00 guard."""
    local = now.astimezone(ZoneInfo('Asia/Shanghai'))
    cutoff = (15, 30) if local.weekday() == 4 else (18, 0)
    return pd.Timestamp(local.date()) - pd.Timedelta(days=0 if (local.hour, local.minute) >= cutoff else 1)


def snapshot_is_current(previous, as_of, end):
    """Reuse a verified phase, so successful Friday data is not rewritten on weekends."""
    if previous.get('engineVersion') != ENGINE_VERSION or previous.get('priceAsOf') != iso(end):
        return False
    as_of = pd.Timestamp(as_of)
    thursday = as_of - pd.Timedelta(days=(as_of.weekday() - 3) % 7)
    if thursday >= EFFECTIVE_THURSDAY and pd.Timestamp(previous.get('calculationAsOf', '1900-01-01')) < thursday:
        return False
    latest = previous.get('latestSignal', {})
    if latest.get('status', '').startswith('pending_') and pd.Timestamp(latest['executionDate']) <= end:
        return False
    return bool(previous.get('provenance', {}).get('portfolioAudit', {}).get('passed'))


def signal_sessions(calendar, through, first=None):
    """One Mon-Thu observation per elapsed Thursday; never use a Friday bar."""
    through = pd.Timestamp(through)
    rows = calendar[(calendar.weekday <= 3) & (calendar <= through)]
    if first is not None:
        rows = rows[rows >= pd.Timestamp(first)]
    periods = rows.to_period('W-FRI')
    elapsed = periods.end_time.normalize() - pd.Timedelta(days=1) <= through
    rows = rows[elapsed]
    return pd.DatetimeIndex(pd.Series(rows, index=rows).groupby(rows.to_period('W-FRI')).last())


def thursday_indicators(raw_close, calendar, through, cfg):
    dates = signal_sessions(calendar, through, raw_close.index[0])
    closes = raw_close.reindex(dates)
    momentum = legacy.base.momentum_panel(closes, cfg['momentum_window'])
    ma = pd.DataFrame({s: closes[s].dropna().rolling(cfg['trend_ma_weeks'], min_periods=cfg['trend_ma_weeks']).mean().reindex(dates)
                       for s in closes}, index=dates)
    return closes, momentum, ma


def candidates_at(closes, momentum, ma, assets, vol, date, cfg):
    result = []
    for symbol in closes.columns:
        m, price, average = float(momentum.loc[date, symbol]), float(closes.loc[date, symbol]), float(ma.loc[date, symbol])
        scale, sigma = float(assets.loc[date, symbol]), float(vol.loc[date, symbol])
        if not all(np.isfinite([m, price, average, scale, sigma])) or average <= 0:
            raise ValueError('Signal inputs contain missing warmup, price or scale data')
        result.append(dict(symbol=symbol, name=legacy.NAMES[symbol], code=symbol[2:], momentum=m,
                           maDistance=price / average - 1, assetsYi=scale, volatility=sigma,
                           eligible=bool(m > 0 and price > average and scale > cfg['minimum_net_assets_yi']),
                           theoreticalWeight=risk_weight(sigma, cfg['target_vol'])))
    return sorted(result, key=lambda row: -row['momentum'])


def account_at(cash, units, prices):
    values = units * prices
    equity = float(cash + values.sum())
    active = units > 1e-7
    if active.sum() > 1 or equity <= 0 or cash < -1e-5:
        raise ValueError('Invalid single-ETF account')
    held = units.index[active][0] if active.any() else 'CASH'
    return held, float(values.sum() / equity), equity, values


def make_plan(panel, calendar, decision, cfg, cash, units, vol):
    decision = pd.Timestamp(decision)
    if decision.weekday() != 3:
        raise ValueError('Decisions must be made after Thursday close')
    friday = decision + pd.Timedelta(days=1)
    monday = decision - pd.Timedelta(days=3)
    available = calendar[(calendar >= monday) & (calendar <= decision)]
    if not len(available) or friday not in calendar:
        return dict(decisionDate=iso(decision), signalDate=iso(available[-1]) if len(available) else None,
                    executionDate=iso(friday), executionTiming='friday_close_proxy', status='skipped_holiday',
                    skipReason='friday_closed' if friday not in calendar else 'no_pre_friday_session',
                    target=None, plannedWeight=None, traded=False, selection=False, auditPassed=True), None
    signal = available[-1]
    closes, momentum, ma = thursday_indicators(panel['raw_close'].loc[:signal], calendar, decision, cfg)
    candidates = candidates_at(closes, momentum, ma, panel['assets'], vol, signal, cfg)
    held, weight, _, _ = account_at(cash, units, panel['close'].loc[signal])
    count = len(calendar[(calendar >= pd.Timestamp(cfg['backtest_start'])) & (calendar <= decision)].to_period('W-FRI').unique())
    selection = count % cfg['rebalance_every_weeks'] == 0
    eligible = [c['symbol'] for c in candidates if c['eligible']]
    selected = (eligible[0] if eligible else 'CASH') if selection else (held if held in eligible else 'CASH')
    sigma = float(vol.loc[signal, selected]) if selected != 'CASH' else np.nan
    target = risk_weight(sigma, cfg['target_vol']) if selected != 'CASH' else 0.0
    before = weight if held == selected and selected != 'CASH' else 0.0
    trade, reason = rebalance_gate(before, target, sigma, cfg['target_vol'], cfg['primary_band'], cfg['risk_buffer'], held != selected)
    return dict(decisionDate=iso(decision), signalDate=iso(signal), executionDate=iso(friday),
                executionTiming='friday_close_proxy', asset=selected, previousAsset=held,
                selection=selection, reason='scheduled_selection' if selection else
                'eligibility_exit' if held != 'CASH' and selected == 'CASH' else 'weekly_hold',
                traded=bool(trade), target=float(target), plannedWeight=float(target if trade else before),
                signalWeight=before, previousWeight=weight, sizingReason=reason,
                status='pending_friday_close', auditPassed=True), candidates


def execute_plan(plan, panel, date, cfg, cash, units):
    """Execute the frozen Thursday plan at Friday close; Friday cannot reselect."""
    date = pd.Timestamp(date)
    if date.weekday() != 4 or iso(date) != plan['executionDate'] or pd.Timestamp(plan['signalDate']) >= date:
        raise ValueError('Invalid Friday execution date')
    prices = panel['close'].loc[date]
    _, _, equity, before = account_at(cash, units, prices)
    fills, cost = [], 0.0
    if plan['traded']:
        needed = (units > 1e-12) | ((units.index == plan['asset']) & (plan['target'] > 0))
        if panel['raw_close'].loc[date, needed].isna().any() or not panel['tradable'].loc[date, needed].all():
            raise ValueError('Friday close cannot confirm tradable quotes; publication aborted')
        weights = np.zeros(len(units))
        if plan['asset'] != 'CASH':
            weights[units.index.get_loc(plan['asset'])] = plan['target']
        after, cash, cost = allocate(equity, before.to_numpy(), weights,
                                     (cfg['commission_bps'] + cfg['slippage_bps']) / 10000)
        changes = after - before.to_numpy()
        for col in np.flatnonzero(abs(changes) > 1e-6):
            fills.append(dict(date=date, signal_date=pd.Timestamp(plan['signalDate']), symbol=units.index[col],
                              side='BUY' if changes[col] > 0 else 'SELL', adjusted_price=float(prices.iloc[col]),
                              units_delta=float(changes[col] / prices.iloc[col]), notional=float(abs(changes[col])),
                              cost=float(abs(changes[col]) * (cfg['commission_bps'] + cfg['slippage_bps']) / 10000), phase='close'))
        units = pd.Series(after / prices.to_numpy(), index=units.index)
    held, weight, _, _ = account_at(cash, units, prices)
    result = dict(plan, status='model_executed' if plan['traded'] else 'model_held', actualHolding=held,
                  executionWeight=weight, executionPriceBasis='adjusted_daily_close_proxy')
    return result, cash, units, fills, cost


def legacy_decisions(review, sizing):
    records = []
    for _, row in review.iterrows():
        size = sizing.loc[sizing.date.eq(row.date)].iloc[0]
        records.append(dict(signalDate=iso(row.weekly_signal_date), executionDate=iso(row.date),
            executionTiming='legacy_next_open', asset=row.actual_holding, actualHolding=row.actual_holding,
            previousAsset=row.previous_holding, selection=bool(row.is_selection_day), reason=row.decision_reason,
            sizingReason=size.decision_reason, traded=bool(size.should_rebalance), target=float(size.target_weight),
            plannedWeight=float(size.target_weight if size.should_rebalance else size.signal_weight),
            executionWeight=float(size.actual_open_weight), status='model_executed' if size.should_rebalance else 'model_held', auditPassed=True))
    return records


def audit_combined(panel, cfg, daily, fills, new_plans, calendar):
    """Independently rebuild cash/units and verify all new decision inputs."""
    cash = float(cfg['initial_capital'])
    units = pd.Series(0.0, index=panel['close'].columns)
    rate = (cfg['commission_bps'] + cfg['slippage_bps']) / 10000
    by_date = {date: rows for date, rows in fills.groupby('date')}
    states, errors = {}, []
    for date, row in daily.iterrows():
        for fill in by_date.get(date, pd.DataFrame()).itertuples():
            assert fill.signal_date < fill.date
            assert np.isclose(fill.adjusted_price, panel['close' if fill.phase == 'close' else 'open'].loc[date, fill.symbol])
            assert np.isclose(abs(fill.units_delta) * fill.adjusted_price, fill.notional)
            assert np.isclose(fill.cost, rate * fill.notional)
            cash -= (1 if fill.side == 'BUY' else -1) * fill.notional + fill.cost
            units[fill.symbol] += fill.units_delta
        held, exposure, equity, _ = account_at(cash, units, panel['close'].loc[date])
        assert held == row.holding and abs(cash - row.cash) < 1e-4
        assert np.isclose(exposure, row.exposure, atol=1e-10)
        errors.append(abs(equity - row.equity))
        states[date] = (cash, units.copy())
    assert max(errors) < 1e-4 and np.isclose(daily.cost.sum(), fills.cost.sum(), atol=1e-5)
    for plan in new_plans:
        decision, execution = pd.Timestamp(plan['decisionDate']), pd.Timestamp(plan['executionDate'])
        assert decision.weekday() == 3 and execution == decision + pd.Timedelta(days=1)
        sessions = calendar[(calendar >= decision - pd.Timedelta(days=3)) & (calendar <= decision)]
        if plan['status'] == 'skipped_holiday':
            assert execution not in calendar or not len(sessions)
            assert not any(fills.loc[fills.phase.eq('close'), 'date'].eq(execution))
            continue
        signal = pd.Timestamp(plan['signalDate'])
        assert signal == sessions[-1] and signal < execution
        observation_dates = signal_sessions(calendar, decision, panel['raw_close'].index[0])
        ranked = []
        for symbol in panel['raw_close']:
            observed = panel['raw_close'][symbol].reindex(observation_dates).dropna()
            momentum = float(observed.iloc[-1] / observed.iloc[-cfg['momentum_window'] - 1] - 1)
            average = observed.tail(cfg['trend_ma_weeks']).mean()
            if momentum > 0 and observed.iloc[-1] > average and panel['assets'].loc[signal, symbol] > cfg['minimum_net_assets_yi']:
                ranked.append((symbol, momentum))
        ranked.sort(key=lambda item: -item[1])
        prior_cash, prior_units = states[signal]
        held, weight, _, _ = account_at(prior_cash, prior_units, panel['close'].loc[signal])
        selection = len(calendar[(calendar >= pd.Timestamp(cfg['backtest_start'])) & (calendar <= decision)].to_period('W-FRI').unique()) % cfg['rebalance_every_weeks'] == 0
        eligible = [row[0] for row in ranked]
        expected = (eligible[0] if eligible else 'CASH') if selection else (held if held in eligible else 'CASH')
        assert plan['selection'] == selection and plan['asset'] == expected
        if expected == 'CASH':
            target, sigma, before = 0.0, np.nan, 0.0
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
                assert np.isclose(plan['executionWeight'], target, atol=1e-9)
    return dict(passed=True, max_equity_error_cny=max(errors), fills=len(fills), new_decisions=len(new_plans),
                causalThursdayInputs=True, fridayCloseProxy=True)


def build_record(seed, directory, calendar, as_of):
    as_of = pd.Timestamp(as_of)
    end = calendar[calendar <= as_of][-1]
    if calendar[-1] < as_of + pd.Timedelta(days=10):
        raise ValueError('Exchange calendar does not cover the upcoming decision and execution dates')
    cfg = dict(seed['config'], symbols=legacy.SYMBOLS, weekly_exit_on_ineligible=True, last_week_complete=False)
    legacy.base.DATA = directory
    panel, _, manifest, _, _ = legacy.base.load_market(cfg, iso(end))
    cutoff = min(end, EFFECTIVE_THURSDAY)
    old_panel, _, _, _, _ = legacy.base.load_market(cfg, iso(cutoff))
    start = int(old_panel['close'].index.searchsorted(pd.Timestamp(cfg['backtest_start'])))
    full, old_fills, review = legacy.base.simulate_weekly(old_panel, cfg, start, allowed=legacy.SYMBOLS)
    selection_audit = legacy.base.audit(old_panel, cfg, full, old_fills, review, allowed=legacy.SYMBOLS)
    old_vol = volatility_panel(old_panel['raw_close'], cfg['volatility_window'])
    options = dict(target=cfg['target_vol'], band=cfg['primary_band'], risk_buffer=cfg['risk_buffer'])
    daily, fills, sizing = simulate(old_panel, cfg, start, review, old_vol, **options)
    audit(old_panel, cfg, start, review, daily, fills, sizing, cfg['volatility_window'], **options)
    baseline = pd.read_csv(io.StringIO(seed['acceptedDaily']), parse_dates=['date']).set_index('date')
    np.testing.assert_allclose(daily.loc[baseline.index, 'nav'], baseline.nav, rtol=1e-10, atol=1e-12)
    np.testing.assert_allclose(daily.loc[baseline.index, 'exposure'], baseline.exposure, rtol=1e-10, atol=1e-12)
    assert daily.loc[baseline.index, 'holding'].equals(baseline.holding)
    fills = fills.assign(phase='open')
    decisions = legacy_decisions(review, sizing)
    latest = dict(decisions[-1])
    confirmed = dict(latest)
    signal_date = pd.Timestamp(latest['signalDate'])
    candidates = candidates_at(old_panel['weekly_close'], old_panel['weekly_momentum'], old_panel['weekly_ma'],
                               old_panel['assets'], old_vol, signal_date, cfg)
    cash = float(daily.cash.iloc[-1])
    units = pd.Series(0.0, index=panel['close'].columns)
    if daily.holding.iloc[-1] != 'CASH':
        held = daily.holding.iloc[-1]
        units[held] = (daily.equity.iloc[-1] - cash) / panel['close'].loc[daily.index[-1], held]
    previous = float(daily.equity.iloc[-1])
    new_rows, new_fills, plans = [], [], []
    vol = volatility_panel(panel['raw_close'], cfg['volatility_window'])
    for date in pd.date_range(EFFECTIVE_THURSDAY, as_of):
        fee, turnover = 0.0, 0.0
        if date.weekday() == 3:
            latest, current_candidates = make_plan(panel, calendar, date, cfg, cash, units, vol)
            plans.append(latest)
            if current_candidates is not None:
                candidates, signal_date = current_candidates, pd.Timestamp(latest['signalDate'])
        if date.weekday() == 4 and latest.get('status') == 'pending_friday_close' and iso(date) == latest['executionDate'] and date <= end:
            latest, cash, units, executed_fills, fee = execute_plan(latest, panel, date, cfg, cash, units)
            new_fills.extend(executed_fills)
            turnover = sum(fill['notional'] for fill in executed_fills)
            plans[-1] = latest
            decisions.append(latest)
            confirmed = dict(latest)
        if date in panel['close'].index and date > daily.index[-1] and date <= end:
            held, exposure, equity, _ = account_at(cash, units, panel['close'].loc[date])
            new_rows.append(dict(date=date, equity=equity, nav=equity / cfg['initial_capital'],
                **{'return': equity / previous - 1}, cash=cash, cash_weight=cash / equity, exposure=exposure,
                cost=fee, turnover=turnover / (equity + fee), holding=held, held_price_missing=False))
            previous = equity
    if new_rows:
        daily = pd.concat([daily, pd.DataFrame(new_rows).set_index('date')])
    if new_fills:
        fills = pd.concat([fills, pd.DataFrame(new_fills)], ignore_index=True)
    portfolio_audit = audit_combined(panel, cfg, daily, fills, plans, calendar)
    # Before the new policy starts, preserve any genuinely pending legacy plan.
    old_end, old_execution = legacy.complete_week(calendar, as_of)
    if as_of < EFFECTIVE_THURSDAY and old_execution > end and old_execution < EFFECTIVE_THURSDAY:
        old_cfg = dict(cfg, last_week_complete=True)
        pending_panel, _, _, _, _ = legacy.base.load_market(old_cfg, iso(old_end))
        latest, candidates = legacy.next_signal(pending_panel, old_cfg, daily.loc[:old_end], vol, old_execution)
        latest['executionTiming'] = 'legacy_next_open'
        signal_date = old_end
    history, peak = [], 1.0
    for date, row in daily.iterrows():
        peak = max(peak, float(row.nav))
        entry = dict(date=iso(date), nav=float(row.nav), exposure=float(row.exposure), drawdown=float(row.nav / peak - 1))
        for symbol in legacy.SYMBOLS:
            entry[symbol] = float(panel['close'].loc[date, symbol] / panel['close'].iloc[start - 1][symbol])
        history.append(entry)
    next_thursday = as_of + pd.Timedelta(days=(3 - as_of.weekday()) % 7 or 7)
    next_thursday = max(next_thursday, EFFECTIVE_THURSDAY)
    next_friday = next_thursday + pd.Timedelta(days=1)
    # The next scheduled attempt is Thu night, Fri before-tail, or Fri after-close.
    next_refresh = (as_of + pd.Timedelta(days=1)) if as_of.weekday() == 3 and latest['status'] == 'pending_friday_close' else next_thursday
    return dict(schemaVersion=2, strategyId=STRATEGY_ID, engineVersion=ENGINE_VERSION, calculationAsOf=iso(as_of), title='双市场趋势', mode='automated_thursday_friday',
        generatedAt=datetime.now(ZoneInfo('UTC')).isoformat(), priceAsOf=iso(end), signalAsOf=iso(signal_date),
        positionAsOf=confirmed['executionDate'], positionTiming=confirmed['executionTiming'],
        executionDate=confirmed['executionDate'], holding=confirmed['actualHolding'], exposure=float(confirmed['executionWeight']),
        latestSignal=latest, pendingSignal=latest if latest['status'].startswith('pending_') else None,
        lastTarget=float(confirmed['target']), lastSizingReason=confirmed['sizingReason'], nav=float(daily.nav.iloc[-1]),
        summary=metrics(daily, 252), annual=legacy.annual_drawdown(daily), candidates=candidates, history=history,
        decisions=list(reversed(decisions[-52:])), nextSignalDate=iso(next_thursday), nextExecutionDate=iso(next_friday),
        nextRefreshExpected=iso(next_refresh),
        timing=dict(effectiveDecisionDate=iso(EFFECTIVE_THURSDAY), firstExecutionDate=iso(EFFECTIVE_THURSDAY + pd.Timedelta(days=1)),
                    decision='thursday_after_close', execution='friday_tail', modelFill='friday_daily_close_proxy',
                    positionRefresh='after_friday_execution', holidayPolicy='skip_week_when_friday_closed_or_no_mon_thu_quote'),
        rules=dict(momentumWeeks=26, maWeeks=40, selectionWeeks=4, targetVol=.2, volatilityDays=63, band=.05, riskLimit=.22, minAssetsYi=10),
        provenance=dict(priceHashes=manifest['hashes'], selectionAudit=selection_audit, portfolioAudit=portfolio_audit,
            acceptedBaselineIdentical=True, acceptedBaselineThrough=seed['anchor'],
            legacyTimingThrough=iso(cutoff), calendarHash=manifest['calendarSha256'], priceCrosscheck=manifest['priceCrosscheck'],
            dataSources=[legacy.source.SINA_URL, legacy.source.SINA_FACTOR_URL, legacy.source.SCALE_URL, legacy.CALENDAR_URL,
                         'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, required=True)
    parser.add_argument('--seed', type=Path, default=Path(__file__).with_name('weekly_seed.enc.json'))
    parser.add_argument('--as-of')
    parser.add_argument('--direct-network', action='store_true')
    parser.add_argument('--previous', type=Path, default=Path('data.enc.json'))
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    password = os.environ.get('APP_PASSWORD', '')
    if not password:
        raise ValueError('APP_PASSWORD must be supplied through the Actions secret')
    envelope = json.loads(args.seed.read_text(encoding='utf8'))
    seed = unseal(envelope, password)
    if args.direct_network:
        legacy.source.session().trust_env = False
    now = datetime.now(ZoneInfo('Asia/Shanghai'))
    safe_day = safe_quote_day(now)
    as_of = pd.Timestamp(args.as_of) if args.as_of else safe_day
    if as_of > safe_day:
        raise ValueError('Cannot use unfinished or future daily quotes')
    calendar, _ = legacy.fetch_calendar()
    end = calendar[calendar <= as_of][-1]
    if end < pd.Timestamp(seed['anchor']):
        raise ValueError('Quotes precede the accepted baseline')
    if args.previous.exists() and not args.force:
        previous = unseal(json.loads(args.previous.read_text(encoding='utf8')), password)
        if snapshot_is_current(previous, as_of, end):
            args.site.mkdir(parents=True, exist_ok=True)
            shutil.copy2(args.previous, args.site / 'data.enc.json')
            print(f"PASS: existing verified phase through {previous['priceAsOf']} reused; no position change or redundant encryption.")
            return
    workroot = args.site.resolve().parent / '.weekly-work'
    workroot.mkdir(parents=True, exist_ok=True)
    directory = workroot / ('run-' + uuid.uuid4().hex)
    directory.mkdir()
    try:
        assert directory.resolve().is_relative_to(workroot.resolve())
        try:
            legacy.fetch_snapshot(seed, directory, calendar, end)
        except ValueError as error:
            if 'quotes lag' not in str(error):
                raise
            dates = {symbol: str(pd.read_csv(directory / 'prices' / f'{symbol}.csv', usecols=['date']).date.iloc[-1])
                     for symbol in legacy.SYMBOLS + [legacy.CALENDAR_SYMBOL]}
            raise ValueError(f'Expected closed quotes through {iso(end)}; latest provider dates {dates}. Keep previous confirmed snapshot and retry.') from None
        record = build_record(seed, directory, calendar, as_of)
        args.site.mkdir(parents=True, exist_ok=True)
        write_sealed(args.site / 'data.enc.json', record, password, salt=base64.b64decode(envelope['salt']))
    finally:
        assert directory.resolve().is_relative_to(workroot.resolve())
        shutil.rmtree(directory)
    message = f"PASS: quotes {record['priceAsOf']}; confirmed model position {record['positionAsOf']}; Thursday/Friday timing and account audits passed."
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf8') as file:
            file.write(message + '\n')


if __name__ == '__main__':
    try:
        main()
    except AssertionError:
        raise SystemExit('Historical baseline or Thursday/Friday audit failed; publication aborted.') from None
