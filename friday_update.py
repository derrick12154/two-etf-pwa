"""Live no-AUM M26/sigma63 ranking with four independently compounded sleeves.

The immutable v2 engine owns history before the policy transition. No broker API.
"""
from __future__ import annotations
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
import friday_update_v2 as old
from sealed_data import unseal, write_sealed
from strategy import metrics

# Preserve the published Oct22 plan. First new policy decision is next Thursday.
EFFECTIVE_THURSDAY = pd.Timestamp('2026-10-29')
ENGINE_VERSION = 'risk-blend-2026-10-23.1'
STRATEGY_ID = 'two-etf-risk-blend-v3'
SYMBOLS = ['sz159915', 'sh513100']
for _name in ('legacy','simulate','audit','volatility_panel','risk_weight','rebalance_gate','iso',
              'safe_quote_day','signal_sessions','thursday_indicators','account_at','make_plan','execute_plan'):
    globals()[_name] = getattr(old, _name)


def policy():
    return dict(id=STRATEGY_ID,effectiveDecisionDate=iso(EFFECTIVE_THURSDAY),
                firstExecutionDate=iso(EFFECTIVE_THURSDAY+pd.Timedelta(days=1)),
                selectionRanking='momentum26_over_volatility63',sizeGateEnabled=False,sleeveCount=4,
                phaseAnchor='2020-01-06',initialSplit='equal_cash_and_units',restoreEqualWeights=False,
                portfolioReleverage=False,netTradeFees=False)


def candidate_scores(panel,calendar,decision,cfg,vol):
    available=calendar[(calendar>=decision-pd.Timedelta(days=3))&(calendar<=decision)]
    signal=available[-1]
    closes,momentum,ma=thursday_indicators(panel['raw_close'].loc[:signal],calendar,decision,cfg)
    rows=[]
    for symbol in panel['raw_close']:
        price,m,average,sigma=map(float,[closes.loc[signal,symbol],momentum.loc[signal,symbol],ma.loc[signal,symbol],vol.loc[signal,symbol]])
        if not np.isfinite([price,m,average,sigma]).all() or average<=0 or sigma<0:
            raise ValueError('Missing or invalid risk-ranking observations; retaining verified snapshot')
        eligible=bool(m>0 and price>average)
        if eligible and sigma<=0:
            raise ValueError('Eligible asset needs positive volatility for risk ranking')
        rows.append(dict(symbol=symbol,name=legacy.NAMES[symbol],code=symbol[2:],momentum=m,
                         maDistance=price/average-1,volatility=sigma,riskScore=m/sigma if sigma>0 else None,
                         eligible=eligible,theoreticalWeight=risk_weight(sigma,.2)))
    return sorted(rows,key=lambda c:(-(c['riskScore'] if c['riskScore'] is not None else -np.inf),-c['momentum']))


def sleeve_plan(panel,calendar,decision,cfg,cash,units,phase,vol,candidates=None):
    decision=pd.Timestamp(decision);friday=decision+pd.Timedelta(days=1)
    if decision.weekday()!=3:raise ValueError('Thursday decision required')
    available=calendar[(calendar>=decision-pd.Timedelta(days=3))&(calendar<=decision)]
    common=dict(decisionDate=iso(decision),signalDate=iso(available[-1]) if len(available) else None,
                executionDate=iso(friday),executionTiming='friday_close_proxy',phase=phase,
                rankingMethod='momentum26_over_volatility63',auditPassed=True)
    if not len(available) or friday not in calendar:
        return dict(common,status='skipped_holiday',skipReason='friday_closed' if friday not in calendar else 'no_pre_friday_session',
                    target=None,plannedWeight=None,traded=False,selection=False)
    signal=available[-1]
    candidates=candidate_scores(panel,calendar,decision,cfg,vol) if candidates is None else candidates
    eligible=[c['symbol'] for c in candidates if c['eligible']]
    count=len(calendar[(calendar>=pd.Timestamp(cfg['backtest_start']))&(calendar<=decision)].to_period('W-FRI').unique())
    selection=count%4==phase
    held,weight,equity,_=account_at(cash,units,panel['close'].loc[signal])
    asset=(eligible[0] if eligible else 'CASH') if selection else (held if held in eligible else 'CASH')
    sigma=float(vol.loc[signal,asset]) if asset!='CASH' else np.nan
    target=risk_weight(sigma,.2) if asset!='CASH' else 0.
    before=weight if asset==held and asset!='CASH' else 0.
    traded,reason=rebalance_gate(before,target,sigma,.2,.05,.1,held!=asset)
    return dict(common,status='pending_friday_close',asset=asset,previousAsset=held,selection=bool(selection),
                observedWeekCount=count,target=float(target),plannedWeight=float(target if traded else before),
                traded=bool(traded),sizingReason=reason,signalWeight=float(before),previousWeight=float(weight),
                signalEquity=float(equity),reason='scheduled_selection' if selection else 'eligibility_exit' if held!='CASH' and asset=='CASH' else 'weekly_hold')


def allocations(cash,units,prices):
    values=units*prices;equity=float(cash+values.sum())
    if equity<=0 or cash < -1e-5 or units.lt(-1e-7).any():raise ValueError('Invalid aggregate account')
    return {s:float(max(0.,values[s]/equity)) for s in SYMBOLS}|{'CASH':float(max(0.,cash/equity))}


def label(weights):
    active=[s for s in SYMBOLS if weights[s]>1e-10]
    return 'MIXED' if len(active)>1 else active[0] if active else 'CASH'


def aggregate_plan(plans):
    if all(p['status']=='skipped_holiday' for p in plans):
        return dict(plans[0],phase=None,sleeves=plans)
    total=sum(p['signalEquity'] for p in plans)
    planned={s:0. for s in SYMBOLS+['CASH']};theory=planned.copy()
    for p in plans:
        fraction=p['signalEquity']/total
        for weights,key in ((planned,'plannedWeight'),(theory,'target')):
            if p['asset']!='CASH':weights[p['asset']]+=fraction*p[key]
            weights['CASH']+=fraction*(1-p[key])
    return dict(decisionDate=plans[0]['decisionDate'],signalDate=plans[0]['signalDate'],executionDate=plans[0]['executionDate'],
                executionTiming='friday_close_proxy',status='pending_friday_close',asset=label(planned),
                target=1-theory['CASH'],plannedWeight=1-planned['CASH'],plannedWeights=planned,theoreticalWeights=theory,
                traded=any(p['traded'] for p in plans),selection=any(p['selection'] for p in plans),
                selectedPhases=[p['phase'] for p in plans if p['selection']],sizingReason='four_phase',reason='four_phase_review',
                rankingMethod='momentum26_over_volatility63',sleeves=plans,auditPassed=True)


def simulate_sleeves(panel,calendar,cfg,as_of,initial_cash,initial_units):
    cash=[initial_cash/4]*4;units=[initial_units/4 for _ in range(4)]
    vol=volatility_panel(panel['raw_close'],63);pending=None;candidates=[]
    daily=[];fills=[];plans=[];states=[]
    for date in pd.date_range(EFFECTIVE_THURSDAY,as_of):
        fee=notional=0.
        if date.weekday()==3:
            available=calendar[(calendar>=date-pd.Timedelta(days=3))&(calendar<=date)]
            valid=len(available) and date+pd.Timedelta(days=1) in calendar
            if valid:candidates=candidate_scores(panel,calendar,date,cfg,vol)
            pending=aggregate_plan([sleeve_plan(panel,calendar,date,cfg,cash[i],units[i],i,vol,candidates if valid else None) for i in range(4)])
            plans.append(pending)
        if date.weekday()==4 and pending is not None and pending['status']=='pending_friday_close' and date in panel['close'].index:
            confirmed=[]
            for i,p in enumerate(pending['sleeves']):
                done,cash[i],units[i],trades,cost=execute_plan(p,panel,date,cfg,cash[i],units[i])
                fee+=cost;notional+=sum(t['notional'] for t in trades)
                for t in trades:t['sleevePhase']=i
                fills.extend(trades);confirmed.append(done)
            total_cash=sum(cash);total_units=sum(units)
            weights=allocations(total_cash,total_units,panel['close'].loc[date])
            pending=dict(pending,status='model_executed' if pending['traded'] else 'model_held',sleeves=confirmed,
                         actualHolding=label(weights),executionWeight=1-weights['CASH'],executionWeights=weights,
                         executionPriceBasis='adjusted_daily_close_proxy')
            plans[-1]=pending
        if date in panel['close'].index:
            total_cash=sum(cash);total_units=sum(units);equity=total_cash+(total_units*panel['close'].loc[date]).sum()
            weights=allocations(total_cash,total_units,panel['close'].loc[date])
            daily.append(dict(date=date,equity=float(equity),cash=float(total_cash),exposure=1-weights['CASH'],cost=fee,
                              turnover=notional/(equity+fee),weights=weights))
            states.append([dict(phase=i,cash=float(cash[i]),units={s:float(units[i][s]) for s in SYMBOLS}) for i in range(4)])
    return daily,fills,plans,candidates,states


def audit_sleeves(panel,calendar,cfg,daily,fills,plans,initial_cash,initial_units,states):
    """Independent prefix signals, analytic allocations, gross trade and cash ledger."""
    cash=[initial_cash/4]*4;units=[initial_units/4 for _ in range(4)]
    ledger_cash=cash.copy();ledger_units=[u.copy() for u in units]
    by_decision={pd.Timestamp(p['decisionDate']):p for p in plans}
    by_execution={pd.Timestamp(p['executionDate']):p for p in plans}
    by_date={pd.Timestamp(row['date']):(row,states[i]) for i,row in enumerate(daily)}
    rate=(cfg['commission_bps']+cfg['slippage_bps'])/10000;maximum=0.;decisions=0
    for date in pd.date_range(EFFECTIVE_THURSDAY,pd.Timestamp(daily[-1]['date'])):
        expected_fee=0.
        if date in by_decision:
            plan=by_decision[date];available=calendar[(calendar>=date-pd.Timedelta(days=3))&(calendar<=date)]
            if plan['status']=='skipped_holiday':
                assert not len(available) or date+pd.Timedelta(days=1) not in calendar
            else:
                signal=available[-1];observed=signal_sessions(calendar,date,panel['raw_close'].index[0]);ranked=[];sigmas={}
                for symbol in panel['raw_close']:
                    samples=panel['raw_close'][symbol].reindex(observed).dropna()
                    m=float(samples.iloc[-1]/samples.iloc[-27]-1);ma=float(samples.tail(40).mean())
                    ret=panel['raw_close'].loc[:signal,symbol].dropna().pct_change(fill_method=None).dropna()
                    sigma=float(ret.tail(63).std(ddof=1)*np.sqrt(252));sigmas[symbol]=sigma
                    assert np.isfinite([samples.iloc[-1],m,ma,sigma]).all()
                    if m>0 and samples.iloc[-1]>ma:
                        assert sigma>0;ranked.append((symbol,m/sigma,m))
                ranked.sort(key=lambda row:(-row[1],-row[2]));eligible=[row[0] for row in ranked]
                count=len(calendar[(calendar>=pd.Timestamp(cfg['backtest_start']))&(calendar<=date)].to_period('W-FRI').unique())
                for i,p in enumerate(plan['sleeves']):
                    values=units[i]*panel['close'].loc[signal];equity=cash[i]+values.sum()
                    held=units[i].index[units[i].gt(1e-7)][0] if units[i].gt(1e-7).any() else 'CASH'
                    selected=(eligible[0] if eligible else 'CASH') if count%4==i else (held if held in eligible else 'CASH')
                    target=min(1.,.2/sigmas[selected]) if selected!='CASH' else 0.
                    before=float(values.sum()/equity) if held==selected and selected!='CASH' else 0.
                    risk=selected!='CASH' and before>target and before*sigmas[selected]>.22+1e-12
                    trade=held!=selected or risk or abs(before-target)>=.05-1e-12
                    assert p['phase']==i and p['asset']==selected and p['selection']==(count%4==i)
                    assert p['traded']==trade and np.isclose(p['target'],target,atol=1e-11)
                    assert np.isclose(p['plannedWeight'],target if trade else before,atol=1e-11)
                    assert np.isclose(p['signalEquity'],equity,atol=1e-5)
                    decisions+=1
        if date in by_execution and by_execution[date]['status'] in ('model_executed','model_held'):
            for i,p in enumerate(by_execution[date]['sleeves']):
                if not p['traded']:continue
                prices=panel['close'].loc[date];values=units[i]*prices;equity=cash[i]+values.sum();old_value=float(values.sum());w=p['target']
                held=units[i].index[units[i].gt(1e-7)][0] if units[i].gt(1e-7).any() else 'CASH'
                if held==p['asset'] and held!='CASH':
                    post=(equity+rate*old_value)/(1+rate*w) if w*equity>=old_value else (equity-rate*old_value)/(1-rate*w)
                else:post=(equity-rate*old_value)/(1+rate*w)
                target_values=pd.Series(0.,index=SYMBOLS)
                if p['asset']!='CASH':target_values[p['asset']]=post*w
                fee=float((target_values-values).abs().sum()*rate)
                cash[i]=float(equity-fee-target_values.sum());units[i]=target_values/prices;expected_fee+=fee
        for fill in [f for f in fills if pd.Timestamp(f['date'])==date]:
            i=fill['sleevePhase'];symbol=fill['symbol'];assert date.weekday()==4 and pd.Timestamp(fill['signal_date'])<date
            assert panel['tradable'].loc[date,symbol] and np.isclose(fill['adjusted_price'],panel['close'].loc[date,symbol])
            assert np.isclose(fill['notional'],abs(fill['units_delta'])*fill['adjusted_price']) and np.isclose(fill['cost'],fill['notional']*rate)
            ledger_cash[i]-=(1 if fill['side']=='BUY' else -1)*fill['notional']+fill['cost'];ledger_units[i][symbol]+=fill['units_delta']
        if date in by_date:
            row,saved=by_date[date];prices=panel['close'].loc[date]
            for i in range(4):
                maximum=max(maximum,abs(cash[i]-ledger_cash[i]),abs(cash[i]-saved[i]['cash']))
                np.testing.assert_allclose(units[i],ledger_units[i],atol=1e-6,rtol=1e-10)
                np.testing.assert_allclose(units[i],pd.Series(saved[i]['units']).reindex(SYMBOLS),atol=1e-6,rtol=1e-10)
                assert cash[i]>=-1e-5 and units[i].ge(-1e-7).all() and units[i].gt(1e-7).sum()<=1
            total_cash=sum(cash);total_units=sum(units);equity=total_cash+(total_units*prices).sum()
            maximum=max(maximum,abs(equity-row['equity']),abs(total_cash-row['cash']))
            assert np.isclose(row['cost'],expected_fee,atol=1e-6)
            assert np.isclose(row['exposure'],float((total_units*prices).sum()/equity),atol=1e-10)
    assert maximum<1e-4 and np.isclose(sum(d['cost'] for d in daily),sum(f['cost'] for f in fills))
    return dict(passed=True,max_equity_error_cny=maximum,new_decisions=decisions,fills=len(fills),
                causalThursdayInputs=True,fridayCloseProxy=True,fourSleeveLedger=True,grossFees=True,noSizeGate=True)


def build_record(seed,directory,calendar,as_of):
    as_of=pd.Timestamp(as_of)
    if as_of<EFFECTIVE_THURSDAY:
        record=old.build_record(seed,directory,calendar,as_of)
        record.update(engineVersion=ENGINE_VERSION,scheduledPolicy=policy())
        return record
    baseline=old.build_record(seed,directory,calendar,EFFECTIVE_THURSDAY-pd.Timedelta(days=1))
    cfg=dict(seed['config'],symbols=SYMBOLS,weekly_exit_on_ineligible=True,last_week_complete=False)
    legacy.base.DATA=directory
    end=calendar[calendar<=as_of][-1]
    if calendar[-1]<as_of+pd.Timedelta(days=10):raise ValueError('Calendar does not cover next execution')
    panel,_,manifest,_,_=legacy.base.load_market(cfg,iso(end))
    last=baseline['history'][-1];equity=last['nav']*cfg['initial_capital'];initial_cash=equity*(1-last['exposure'])
    initial_units=pd.Series(0.,index=panel['close'].columns)
    if baseline['holding']!='CASH':initial_units[baseline['holding']]=(equity-initial_cash)/panel['close'].loc[last['date'],baseline['holding']]
    daily,fills,plans,candidates,states=simulate_sleeves(panel,calendar,cfg,as_of,initial_cash,initial_units)
    if not daily:raise ValueError('New policy has no confirmed daily observations')
    audit_new=audit_sleeves(panel,calendar,cfg,daily,fills,plans,initial_cash,initial_units,states)
    confirmed=dict(baseline['decisions'][0]);weights={s:baseline['exposure'] if baseline['holding']==s else 0. for s in SYMBOLS}
    weights['CASH']=1-baseline['exposure']
    decisions=list(reversed(baseline['decisions']))
    for p in plans:
        if p['status'] in ('model_executed','model_held'):
            confirmed=p;weights=p['executionWeights'];decisions.append(p)
    history=list(baseline['history']);anchor={s:panel['close'].loc[last['date'],s]/last[s] for s in SYMBOLS};peak=max(1.,max(h['nav'] for h in history))
    for row in daily:
        nav=row['equity']/cfg['initial_capital'];peak=max(peak,nav)
        history.append(dict(date=iso(row['date']),nav=nav,exposure=row['exposure'],drawdown=nav/peak-1,
                            **{s:float(panel['close'].loc[row['date'],s]/anchor[s]) for s in SYMBOLS}))
    df=pd.DataFrame(history).set_index('date');df.index=pd.to_datetime(df.index)
    df['return']=df.nav.pct_change(fill_method=None).fillna(df.nav.iloc[0]-1);df['turnover']=0.;df['cost']=0.
    for row in daily:df.loc[row['date'],['turnover','cost']]=[row['turnover'],row['cost']]
    summary=metrics(df,252)
    summary['total_cost']+=baseline['summary']['total_cost']
    summary['annual_traded_notional_over_nav']+=baseline['summary']['annual_traded_notional_over_nav']*baseline['summary']['days']/len(df)
    latest=plans[-1];next_thursday=as_of+pd.Timedelta(days=(3-as_of.weekday())%7 or 7)
    next_refresh=as_of+pd.Timedelta(days=1) if as_of.weekday()==3 and latest['status']=='pending_friday_close' else next_thursday
    sleeve_states=states[-1];current_equities=[s['cash']+sum(s['units'][symbol]*panel['close'].loc[end,symbol] for symbol in SYMBOLS) for s in sleeve_states]
    for i,s in enumerate(sleeve_states):s.update(equity=float(current_equities[i]),equityWeight=float(current_equities[i]/sum(current_equities)))
    return dict(schemaVersion=3,strategyId=STRATEGY_ID,engineVersion=ENGINE_VERSION,mode='automated_four_phase',title='双市场趋势',
                generatedAt=datetime.now(ZoneInfo('UTC')).isoformat(),calculationAsOf=iso(as_of),priceAsOf=iso(end),
                signalAsOf=latest.get('signalDate') or baseline['signalAsOf'],
                candidateAsOf=next((p['signalDate'] for p in reversed(plans) if p['status']!='skipped_holiday'),baseline['signalAsOf']),positionAsOf=confirmed['executionDate'],
                positionTiming=confirmed.get('executionTiming','legacy_next_open'),executionDate=confirmed['executionDate'],
                holding=label(weights),exposure=1-weights['CASH'],confirmedWeights=weights,
                latestSignal=latest,pendingSignal=latest if latest['status'].startswith('pending_') else None,
                lastTarget=confirmed['target'],lastSizingReason=confirmed['sizingReason'],nav=history[-1]['nav'],
                summary=summary,annual=legacy.annual_drawdown(df),history=history,candidates=candidates or baseline['candidates'],
                decisions=list(reversed(decisions[-52:])),sleeves=sleeve_states,policy=policy(),
                nextSignalDate=iso(next_thursday),nextExecutionDate=iso(next_thursday+pd.Timedelta(days=1)),nextRefreshExpected=iso(next_refresh),
                timing=dict(effectiveDecisionDate=iso(EFFECTIVE_THURSDAY),firstExecutionDate=iso(EFFECTIVE_THURSDAY+pd.Timedelta(days=1)),
                            decision='thursday_after_close',execution='friday_tail',modelFill='friday_daily_close_proxy',positionRefresh='after_friday_execution'),
                rules=dict(momentumWeeks=26,maWeeks=40,selectionWeeks=4,targetVol=.2,volatilityDays=63,band=.05,riskLimit=.22,
                           minAssetsYi=None,sizeGateEnabled=False,selectionRanking='momentum26_over_volatility63',sleeveCount=4),
                provenance=dict(priceHashes=manifest['hashes'],selectionAudit=dict(passed=True,independentPrefixDecisions=audit_new['new_decisions']),
                                portfolioAudit=audit_new,acceptedBaselineIdentical=True,acceptedBaselineThrough=seed['anchor'],
                                historicalPolicyThrough=last['date'],transitionEquity=equity,transitionCash=initial_cash,
                                transitionUnits={s:float(initial_units[s]) for s in SYMBOLS},calendarHash=manifest['calendarSha256'],
                                priceCrosscheck=manifest['priceCrosscheck'],legacyPortfolioAudit=baseline['provenance']['portfolioAudit']))


def snapshot_is_current(previous,as_of,end):
    if previous.get('engineVersion')!=ENGINE_VERSION or previous.get('priceAsOf')!=iso(end):return False
    date=pd.Timestamp(as_of);thursday=date-pd.Timedelta(days=(date.weekday()-3)%7)
    if thursday>=old.EFFECTIVE_THURSDAY and pd.Timestamp(previous.get('calculationAsOf','1900-01-01'))<thursday:return False
    if date>=EFFECTIVE_THURSDAY and previous.get('schemaVersion')!=3:return False
    latest=previous.get('latestSignal',{})
    if latest.get('status','').startswith('pending_') and pd.Timestamp(latest['executionDate'])<=end:return False
    return bool(previous.get('provenance',{}).get('portfolioAudit',{}).get('passed'))


def fetch_snapshot(seed,directory,calendar,end):
    """Fetch prices; archived scale is used only by immutable pre-transition history."""
    legacy.source.DATA=directory;records=[];frames={};hashes={}
    for symbol in SYMBOLS+[legacy.CALENDAR_SYMBOL]:
        meta=legacy.source.fetch_sina_prices({'symbol':symbol},iso(end))
        frame=pd.read_csv(directory/'prices'/f'{symbol}.csv',parse_dates=['date']).set_index('date')
        accepted=pd.read_csv(io.StringIO(seed['prices'][symbol]),parse_dates=['date']).set_index('date')
        if not accepted.index.isin(frame.index).all():raise ValueError('Source removed accepted historical bars')
        np.testing.assert_allclose(frame.loc[accepted.index,['open','close','high','low']],accepted[['open','close','high','low']],rtol=1e-8,atol=1e-8)
        frames[symbol]=frame;hashes[symbol]=meta['sha256']
        if symbol in SYMBOLS:records.append(dict(code=symbol[2:],symbol=symbol,**{k:v for k,v in meta.items() if k!='symbol'}))
    legacy.check_recent_prices(frames,calendar,end,seed['anchor'])
    pd.DataFrame(records).to_csv(directory/'universe.csv',index=False)
    scales=pd.read_csv(io.StringIO(seed['scaleHistory']),parse_dates=['report_date'])
    cutoff=min(pd.Timestamp(end),EFFECTIVE_THURSDAY-pd.Timedelta(days=1))
    required=(cutoff-pd.Timedelta(days=100)).to_period('Q').start_time-pd.Timedelta(days=1)
    for symbol in SYMBOLS:
        if scales.loc[scales.symbol.eq(symbol),'report_date'].max()<required:
            raise ValueError('Archived scale is insufficient to reproduce pre-transition history')
    scales.to_csv(directory/'scale_history.csv',index=False,date_format='%Y-%m-%d')
    manifest=dict(hashes=hashes,benchmark_sha256=hashes[legacy.CALENDAR_SYMBOL],
                  calendarSha256=hashlib.sha256('|'.join(map(str,calendar)).encode()).hexdigest(),
                  priceCrosscheck=legacy.crosscheck_prices(frames,end),scalePolicy='archived_for_pretransition_only')
    (directory/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--site',type=Path,required=True)
    parser.add_argument('--seed',type=Path,default=Path(__file__).with_name('weekly_seed.enc.json'))
    parser.add_argument('--as-of');parser.add_argument('--previous',type=Path,default=Path('data.enc.json'))
    parser.add_argument('--force',action='store_true');parser.add_argument('--direct-network',action='store_true');args=parser.parse_args()
    password=os.environ.get('APP_PASSWORD','')
    if not password:raise ValueError('APP_PASSWORD must come from Actions secret')
    envelope=json.loads(args.seed.read_text(encoding='utf-8'));seed=unseal(envelope,password)
    if args.direct_network:legacy.source.session().trust_env=False
    safe=safe_quote_day(datetime.now(ZoneInfo('Asia/Shanghai')));as_of=pd.Timestamp(args.as_of) if args.as_of else safe
    if as_of>safe:raise ValueError('Cannot use unfinished/future quotes')
    calendar,_=legacy.fetch_calendar();end=calendar[calendar<=as_of][-1]
    if end<pd.Timestamp(seed['anchor']):raise ValueError('Quotes precede baseline')
    if args.previous.exists() and not args.force:
        previous=unseal(json.loads(args.previous.read_text(encoding='utf-8')),password)
        if snapshot_is_current(previous,as_of,end):
            args.site.mkdir(parents=True,exist_ok=True);shutil.copy2(args.previous,args.site/'data.enc.json')
            print('PASS: reuse latest verified policy snapshot');return
    workroot=args.site.resolve().parent/'.weekly-work';workroot.mkdir(parents=True,exist_ok=True)
    directory=workroot/('risk-blend-'+uuid.uuid4().hex);directory.mkdir()
    try:
        assert directory.resolve().is_relative_to(workroot.resolve())
        fetch_snapshot(seed,directory,calendar,end);record=build_record(seed,directory,calendar,as_of)
        args.site.mkdir(parents=True,exist_ok=True)
        write_sealed(args.site/'data.enc.json',record,password,salt=base64.b64decode(envelope['salt']))
    finally:
        assert directory.resolve().is_relative_to(workroot.resolve());shutil.rmtree(directory)
    message=f"PASS: quotes {record['priceAsOf']}; position {record['positionAsOf']}; engine {ENGINE_VERSION}; new policy {iso(EFFECTIVE_THURSDAY)}"
    print(message)
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'],'a',encoding='utf-8') as f:f.write(message+'\n')


if __name__=='__main__':
    try:main()
    except AssertionError:raise SystemExit('Baseline, risk-ranking or four-sleeve account audit failed; no publication.') from None
