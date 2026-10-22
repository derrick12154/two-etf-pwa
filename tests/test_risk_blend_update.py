"""Policy transition, independent sleeves, causal prices and no-scale regressions."""
import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,os.environ.get('ETF_ENGINE_PATH',str(ROOT/'pwa/tools')))
if os.environ.get('ETF_ENGINE_PATH'):
    import friday_update as update
else:
    import risk_blend_update as update
import test_friday_update as fixtures


class RiskBlendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.FridayUpdateTests.setUpClass()
        cls.fixture=fixtures.FridayUpdateTests
        cls.cfg,cls.seed,cls.calendar,cls.directory=cls.fixture.cfg,cls.fixture.seed,cls.fixture.calendar,cls.fixture.directory
        cls.old=update.old.build_record(cls.seed,cls.directory,cls.calendar,'2026-10-28')
        cls.thursday=update.build_record(cls.seed,cls.directory,cls.calendar,'2026-10-29')
        cls.friday=update.build_record(cls.seed,cls.directory,cls.calendar,'2026-10-30')
        if os.environ.get('PWA_BLEND_SNAPSHOT_TEST_OUTPUT'):
            Path(os.environ['PWA_BLEND_SNAPSHOT_TEST_OUTPUT']).write_text(json.dumps([cls.thursday,cls.friday],allow_nan=False),encoding='utf-8')

    @classmethod
    def tearDownClass(cls):cls.fixture.tearDownClass()

    def panel(self,end='2026-11-27'):
        update.legacy.base.DATA=self.directory
        return update.legacy.base.load_market(self.cfg,end)[0]

    def test_policy_before_activation_keeps_original_current_week_plan(self):
        record=update.build_record(self.seed,self.directory,self.calendar,'2026-10-23')
        self.assertEqual(record['schemaVersion'],2)
        self.assertEqual(record['scheduledPolicy']['effectiveDecisionDate'],'2026-10-29')
        original=update.old.build_record(self.seed,self.directory,self.calendar,'2026-10-23')
        for key in ('history','latestSignal','decisions','exposure','summary'):
            self.assertEqual(record[key],original[key])

    def test_history_and_nav_are_not_rewritten_or_reset_at_transition(self):
        before=[r for r in self.thursday['history'] if r['date']< '2026-10-29']
        self.assertEqual(before,self.old['history'])
        self.assertEqual(self.thursday['positionAsOf'],self.old['positionAsOf'])
        split=self.thursday['provenance']
        self.assertAlmostEqual(split['transitionEquity'],self.old['nav']*self.cfg['initial_capital'])
        self.assertTrue(self.friday['provenance']['portfolioAudit']['fourSleeveLedger'])
        self.assertEqual(self.friday['schemaVersion'],3)
        json.dumps(self.friday,allow_nan=False)

    def test_split_cash_units_and_capital_are_equal_without_transfer(self):
        t=self.thursday
        for sleeve in t['sleeves']:
            self.assertAlmostEqual(sleeve['cash']*4,t['provenance']['transitionCash'])
            for symbol,value in t['provenance']['transitionUnits'].items():
                self.assertAlmostEqual(sleeve['units'][symbol]*4,value)
        self.assertAlmostEqual(sum(s['equityWeight'] for s in t['sleeves']),1.)
        self.assertEqual(len(t['latestSignal']['selectedPhases']),1)
        self.assertEqual({s['phase'] for s in t['latestSignal']['sleeves']},{0,1,2,3})

    def test_no_scale_gate_or_missing_scale_dependency_in_new_selection(self):
        panel=self.panel();vol=update.volatility_panel(panel['raw_close'],63)
        original=update.candidate_scores(panel,self.calendar,pd.Timestamp('2026-10-29'),self.cfg,vol)
        panel['assets'].loc[:,:]=np.nan
        self.assertEqual(original,update.candidate_scores(panel,self.calendar,pd.Timestamp('2026-10-29'),self.cfg,vol))
        self.assertFalse(self.friday['rules']['sizeGateEnabled'])
        self.assertIsNone(self.friday['rules']['minAssetsYi'])

    def test_friday_future_quotes_cannot_change_thursday_plan_or_past_balances(self):
        panel=self.panel();initial=self.thursday['provenance'];units=pd.Series(initial['transitionUnits'])
        a=update.simulate_sleeves(panel,self.calendar,self.cfg,pd.Timestamp('2026-11-27'),initial['transitionCash'],units)
        changed={key:value.copy() for key,value in panel.items()}
        for field in ('open','close','raw_close'):changed[field].loc[changed[field].index>'2026-10-29']*=3
        z=update.simulate_sleeves(changed,self.calendar,self.cfg,pd.Timestamp('2026-11-27'),initial['transitionCash'],units)
        for key in ('plannedWeights','theoreticalWeights','selectedPhases','target','plannedWeight'):
            self.assertEqual(a[2][0][key],z[2][0][key])
        self.assertEqual(a[0][0],z[0][0])

    def test_friday_confirmation_and_pending_plan_are_consistent(self):
        self.assertEqual(self.thursday['latestSignal']['status'],'pending_friday_close')
        self.assertEqual(self.friday['positionAsOf'],'2026-10-30')
        self.assertIsNone(self.friday['pendingSignal'])
        self.assertEqual(self.friday['confirmedWeights'],self.friday['latestSignal']['executionWeights'])
        self.assertAlmostEqual(sum(self.friday['confirmedWeights'].values()),1.)
        self.assertAlmostEqual(self.friday['exposure'],1-self.friday['confirmedWeights']['CASH'])

    def test_holiday_skips_all_sleeves_without_monday_substitution(self):
        calendar=self.calendar[self.calendar!=pd.Timestamp('2026-10-30')]
        record=update.build_record(self.seed,self.directory,calendar,'2026-10-30')
        self.assertEqual(record['latestSignal']['status'],'skipped_holiday')
        self.assertEqual(record['positionAsOf'],self.old['positionAsOf'])
        self.assertTrue(all(p['status']=='skipped_holiday' for p in record['latestSignal']['sleeves']))

    def test_mixed_asset_plan_uses_equity_weights_not_simple_sleeve_average(self):
        plans=[]
        for phase,(asset,target,equity) in enumerate([('sz159915',.6,400.),('sh513100',1.,300.),('CASH',0.,200.),('sh513100',.8,100.)]):
            plans.append(dict(phase=phase,decisionDate='2026-10-29',signalDate='2026-10-29',executionDate='2026-10-30',
                              status='pending_friday_close',signalEquity=equity,asset=asset,target=target,plannedWeight=target,
                              traded=True,selection=phase==0))
        p=update.aggregate_plan(plans)
        self.assertEqual(p['asset'],'MIXED')
        self.assertAlmostEqual(p['plannedWeights']['sz159915'],.24)
        self.assertAlmostEqual(p['plannedWeights']['sh513100'],.38)
        self.assertAlmostEqual(p['plannedWeights']['CASH'],.38)

    def test_eligible_zero_volatility_fails_closed(self):
        panel=self.panel();vol=update.volatility_panel(panel['raw_close'],63)
        rows=update.candidate_scores(panel,self.calendar,pd.Timestamp('2026-10-29'),self.cfg,vol)
        eligible=next(c['symbol'] for c in rows if c['eligible']);vol.loc['2026-10-29',eligible]=0.
        with self.assertRaises(ValueError):update.candidate_scores(panel,self.calendar,pd.Timestamp('2026-10-29'),self.cfg,vol)

    def test_same_asset_opposing_sleeve_orders_keep_both_fees(self):
        symbols=['sz159915','sh513100'];date=pd.Timestamp('2026-10-30');prices=pd.DataFrame([[10.,20.]],index=[date],columns=symbols)
        panel=dict(close=prices,raw_close=prices,tradable=prices>0)
        units=pd.Series([50.,0.],index=symbols)
        plan=dict(signalDate='2026-10-29',executionDate='2026-10-30',asset='sz159915',target=.2,traded=True)
        _,_,_,sell,fee_sell=update.execute_plan(plan,panel,date,self.cfg,500.,units)
        _,_,_,buy,fee_buy=update.execute_plan(dict(plan,target=.8),panel,date,self.cfg,500.,units)
        self.assertEqual(sell[0]['side'],'SELL');self.assertEqual(buy[0]['side'],'BUY')
        self.assertGreater(fee_sell+fee_buy,abs(sell[0]['notional']-buy[0]['notional'])*.0005)


if __name__=='__main__':unittest.main()
