"""Deterministic synthetic quotes used only for timing/accounting regression tests."""
import hashlib
import io
import json
import os
import sys
import shutil
import unittest
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, os.environ.get('ETF_ENGINE_PATH', str(ROOT / 'pwa/tools')))
import friday_update as update


class FridayUpdateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = ROOT / 'tests' / ('friday-test-' + uuid.uuid4().hex)
        assert cls.directory.resolve().is_relative_to(ROOT.resolve())
        cls.directory.mkdir()
        (cls.directory / 'prices').mkdir()
        cls.calendar = pd.bdate_range('2018-01-02', '2026-12-31')
        cls.cfg = dict(backtest_start='2020-01-06', initial_capital=1_000_000., symbols=update.legacy.SYMBOLS,
            momentum_frequency='weekly', momentum_window=26, trend_ma_weeks=40, rebalance_every_weeks=4,
            minimum_net_assets_yi=10., scale_availability_lag_days=100, commission_bps=3., slippage_bps=2.,
            annualization=252, volatility_window=63, target_vol=.2, primary_band=.05, risk_buffer=.1,
            weekly_exit_on_ineligible=True, last_week_complete=False)
        t = np.arange(len(cls.calendar))
        records, hashes = [], {}
        for index, symbol in enumerate(['sh513100', 'sz159915', 'sh510300']):
            close = np.exp((.0007 if index == 0 else .0003) * t + (.04 if index == 0 else .06) * np.sin(t / (5 + index)))
            frame = pd.DataFrame(dict(date=cls.calendar, open=close * .998, close=close,
                                      high=close * 1.01, low=close * .99, volume=1_000_000.))
            path = cls.directory / 'prices' / (symbol + '.csv')
            frame.to_csv(path, index=False)
            hashes[symbol] = hashlib.sha256(path.read_bytes()).hexdigest()
            if symbol != 'sh510300':
                records.append(dict(code=symbol[2:], symbol=symbol, bars=len(frame), sha256=hashes[symbol]))
        pd.DataFrame(records).to_csv(cls.directory / 'universe.csv', index=False)
        pd.DataFrame([dict(symbol=s, report_date='2010-01-01', net_assets_yi=100.) for s in update.legacy.SYMBOLS]).to_csv(cls.directory / 'scale_history.csv', index=False)
        (cls.directory / 'manifest.json').write_text(json.dumps(dict(hashes=hashes, calendarSha256='synthetic-test-calendar', priceCrosscheck={'syntheticTestOnly': True})))
        update.legacy.base.DATA = cls.directory
        panel, _, _, _, _ = update.legacy.base.load_market(cls.cfg, '2026-10-08')
        start = int(panel['close'].index.searchsorted(pd.Timestamp(cls.cfg['backtest_start'])))
        _, _, review = update.legacy.base.simulate_weekly(panel, cls.cfg, start, allowed=update.legacy.SYMBOLS)
        vol = update.volatility_panel(panel['raw_close'], 63)
        accepted, _, _ = update.simulate(panel, cls.cfg, start, review, vol, target=.2, band=.05, risk_buffer=.1)
        cls.seed = dict(config=cls.cfg, anchor='2026-10-08', acceptedDaily=accepted.to_csv())
        cls.thursday = update.build_record(cls.seed, cls.directory, cls.calendar, '2026-10-22')
        cls.friday = update.build_record(cls.seed, cls.directory, cls.calendar, '2026-10-23')
        if os.environ.get('PWA_SNAPSHOT_TEST_OUTPUT'):
            Path(os.environ['PWA_SNAPSHOT_TEST_OUTPUT']).write_text(json.dumps([cls.thursday, cls.friday], allow_nan=False), encoding='utf8')

    @classmethod
    def tearDownClass(cls):
        assert cls.directory.resolve().is_relative_to(ROOT.resolve())
        shutil.rmtree(cls.directory)

    def test_frozen_history_is_not_rewritten(self):
        accepted = pd.read_csv(io.StringIO(self.seed['acceptedDaily'])).set_index('date')
        history = pd.DataFrame(self.friday['history']).set_index('date').loc[accepted.index]
        np.testing.assert_allclose(history.nav, accepted.nav, rtol=1e-10, atol=1e-12)
        np.testing.assert_allclose(history.exposure, accepted.exposure, rtol=1e-10, atol=1e-12)
        self.assertTrue(self.friday['provenance']['portfolioAudit']['passed'])
        json.dumps(self.friday, allow_nan=False)

    def test_thursday_plan_does_not_change_confirmed_position(self):
        plan = self.thursday['latestSignal']
        self.assertEqual(plan['signalDate'], '2026-10-22')
        self.assertEqual(plan['executionDate'], '2026-10-23')
        self.assertEqual(plan['status'], 'pending_friday_close')
        self.assertEqual(self.thursday['positionAsOf'], '2026-10-19')
        self.assertIsNotNone(self.thursday['pendingSignal'])

    def test_friday_confirmation_updates_position_and_clears_pending(self):
        self.assertEqual(self.friday['positionAsOf'], '2026-10-23')
        self.assertEqual(self.friday['positionTiming'], 'friday_close_proxy')
        self.assertIsNone(self.friday['pendingSignal'])
        self.assertIn(self.friday['latestSignal']['status'], ['model_executed', 'model_held'])
        self.assertAlmostEqual(self.friday['exposure'], self.friday['latestSignal']['executionWeight'])
        self.assertAlmostEqual(self.friday['exposure'], self.friday['history'][-1]['exposure'])

    def test_friday_prices_cannot_revise_thursday_decision(self):
        update.legacy.base.DATA = self.directory
        panel, _, _, _, _ = update.legacy.base.load_market(self.cfg, '2026-10-23')
        units = pd.Series(0., index=panel['close'].columns)
        units['sh513100'] = 750_000 / panel['close'].loc['2026-10-22', 'sh513100']
        vol = update.volatility_panel(panel['raw_close'], 63)
        first, _ = update.make_plan(panel, self.calendar, pd.Timestamp('2026-10-22'), self.cfg, 250_000., units, vol)
        changed = {key: value.copy() for key, value in panel.items()}
        changed['raw_close'].loc['2026-10-23'] *= 30
        changed['close'].loc['2026-10-23'] *= 30
        second, _ = update.make_plan(changed, self.calendar, pd.Timestamp('2026-10-22'), self.cfg, 250_000., units,
                                     update.volatility_panel(changed['raw_close'], 63))
        self.assertEqual(first, second)

    def test_execution_uses_friday_close_and_handles_sell_and_buy_costs(self):
        symbols = ['sh513100', 'sz159915']
        prices = pd.DataFrame([[20., 5.]], index=[pd.Timestamp('2026-10-23')], columns=symbols)
        panel = dict(close=prices, raw_close=prices, tradable=prices.gt(0))
        plan = dict(signalDate='2026-10-22', executionDate='2026-10-23', asset='sz159915', target=.6, traded=True)
        done, cash, units, fills, cost = update.execute_plan(plan, panel, '2026-10-23', self.cfg,
                                                            20_000., pd.Series([4_000., 0.], index=symbols))
        self.assertAlmostEqual(done['executionWeight'], .6)
        self.assertEqual({row['side'] for row in fills}, {'BUY', 'SELL'})
        self.assertTrue(all(row['phase'] == 'close' for row in fills))
        self.assertAlmostEqual(cash + (units * prices.iloc[0]).sum(), 100_000 - cost)

    def test_holidays_do_not_move_execution_to_monday(self):
        calendar = self.calendar[self.calendar != pd.Timestamp('2026-10-23')]
        record = update.build_record(self.seed, self.directory, calendar, '2026-10-23')
        self.assertEqual(record['latestSignal']['status'], 'skipped_holiday')
        self.assertEqual(record['latestSignal']['skipReason'], 'friday_closed')
        self.assertEqual(record['positionAsOf'], '2026-10-19')
        self.assertIsNone(record['pendingSignal'])

    def test_closed_thursday_uses_prior_session_in_same_week(self):
        calendar = self.calendar[self.calendar != pd.Timestamp('2026-10-22')]
        dates = update.signal_sessions(calendar, pd.Timestamp('2026-10-22'), pd.Timestamp('2026-10-01'))
        self.assertEqual(dates[-1], pd.Timestamp('2026-10-21'))
        self.assertTrue(all(dates.weekday <= 3))

    def test_no_pre_friday_session_skips_week(self):
        calendar = self.calendar[~self.calendar.isin(pd.bdate_range('2026-10-19', '2026-10-22'))]
        plan, _ = update.make_plan({}, calendar, pd.Timestamp('2026-10-22'), self.cfg, 1., pd.Series(dtype=float), None)
        self.assertEqual(plan['skipReason'], 'no_pre_friday_session')

    def test_full_holiday_week_does_not_advance_selection_phase(self):
        calendar = pd.bdate_range('2026-03-02', '2026-04-10')
        calendar = calendar[~calendar.isin(pd.bdate_range('2026-03-30', '2026-04-03'))]
        self.assertEqual(len(calendar.to_period('W-FRI').unique()), 5)

    def test_daily_close_is_not_used_during_friday_tail(self):
        self.assertEqual(update.safe_quote_day(datetime(2026, 10, 23, 14, 50, tzinfo=ZoneInfo('Asia/Shanghai'))), pd.Timestamp('2026-10-22'))
        self.assertEqual(update.safe_quote_day(datetime(2026, 10, 23, 15, 37, tzinfo=ZoneInfo('Asia/Shanghai'))), pd.Timestamp('2026-10-23'))

    def test_weekend_reuses_successful_friday_confirmation(self):
        self.assertTrue(update.snapshot_is_current(self.friday, '2026-10-24', pd.Timestamp('2026-10-23')))
        self.assertFalse(update.snapshot_is_current(self.thursday, '2026-10-23', pd.Timestamp('2026-10-23')))
        self.assertFalse(update.snapshot_is_current(self.friday, '2026-10-29', pd.Timestamp('2026-10-29')))


if __name__ == '__main__':
    unittest.main()
