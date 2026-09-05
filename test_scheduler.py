import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd
import pytz

import send_spy_ma_telegram as bot
from send_spy_ma_telegram import (
    assert_recent_session_continuity,
    ensure_session_close,
    frame_through_session,
    latest_completed_market_session,
    regular_close_from_metadata,
    required_data_missing,
    state_reported_market_date,
    trading_days_missed,
)


class SchedulerTests(unittest.TestCase):
    def test_reads_valid_reported_date(self):
        state = {"_meta": {"last_reported_market_date": "2026-09-04"}}
        self.assertEqual(state_reported_market_date(state), date(2026, 9, 4))

    def test_invalid_or_absent_reported_date_is_none(self):
        self.assertIsNone(state_reported_market_date({}))
        self.assertIsNone(state_reported_market_date(
            {"_meta": {"last_reported_market_date": "not-a-date"}}))

    def test_required_sources_must_match_expected_date(self):
        expected = date(2026, 9, 4)
        sources = {
            "SPY": expected,
            "QQQ": date(2026, 9, 3),
            "NASDAQCOM": None,
        }
        self.assertEqual(
            required_data_missing(expected, sources),
            ["QQQ", "NASDAQCOM"],
        )

    def test_preclose_manual_session_excludes_partial_today(self):
        et = pytz.timezone("America/New_York")
        now = et.localize(datetime(2026, 9, 4, 15, 45))
        self.assertEqual(latest_completed_market_session(now), date(2026, 9, 3))

        df = pd.DataFrame(
            {"Close": [100.0, 101.0]},
            index=pd.to_datetime(["2026-09-03", "2026-09-04"]),
        )
        clipped = frame_through_session(df, date(2026, 9, 3))
        self.assertEqual(pd.Timestamp(clipped.index[-1]).date(), date(2026, 9, 3))

    def test_postclose_and_early_close_sessions(self):
        et = pytz.timezone("America/New_York")
        regular = et.localize(datetime(2026, 9, 4, 16, 17))
        early_close = et.localize(datetime(2026, 11, 27, 13, 17))
        self.assertEqual(latest_completed_market_session(regular), date(2026, 9, 4))
        self.assertEqual(latest_completed_market_session(early_close), date(2026, 11, 27))

    def test_exchange_holidays_are_not_federal_holiday_proxy(self):
        # Good Friday is closed; Columbus Day and Veterans Day are open.
        self.assertEqual(trading_days_missed(date(2026, 4, 2), date(2026, 4, 3)), 0)
        self.assertEqual(trading_days_missed(date(2026, 10, 9), date(2026, 10, 12)), 1)
        self.assertEqual(trading_days_missed(date(2026, 11, 10), date(2026, 11, 11)), 1)

    def test_regular_close_metadata_rejects_intraday_or_wrong_session(self):
        good = {"regularMarketPrice": 123.45, "regularMarketTime": 1788552000}
        self.assertEqual(
            regular_close_from_metadata(good, "SPY", date(2026, 9, 4)), 123.45)
        bad = dict(good, regularMarketTime=1788548400)
        with self.assertRaisesRegex(ValueError, "from session close"):
            regular_close_from_metadata(bad, "SPY", date(2026, 9, 4))
        timestamp_form = dict(
            good, regularMarketTime=pd.Timestamp("2026-09-04 16:00", tz="America/New_York"))
        self.assertEqual(
            regular_close_from_metadata(timestamp_form, "SPY", date(2026, 9, 4)),
            123.45,
        )
        preliminary_index = dict(
            good, regularMarketTime=pd.Timestamp("2026-09-04 16:00", tz="America/New_York"))
        with self.assertRaisesRegex(ValueError, "final-correction boundary"):
            regular_close_from_metadata(
                preliminary_index, "^IXIC", date(2026, 9, 4),
                allow_late_index_timestamp=True)
        late_index = dict(
            good, regularMarketTime=pd.Timestamp("2026-09-04 17:15", tz="America/New_York"))
        self.assertEqual(
            regular_close_from_metadata(
                late_index, "^IXIC", date(2026, 9, 4),
                allow_late_index_timestamp=True),
            123.45,
        )

    def test_continuity_gate_rejects_missing_intermediate_session(self):
        cal = bot.xnyse_calendar()
        sessions = cal.sessions_in_range("2026-08-01", "2026-09-04")
        idx = pd.DatetimeIndex(sessions).tz_localize(None)
        complete = pd.DataFrame({"Close": np.arange(len(idx), dtype=float) + 100}, index=idx)
        self.assertTrue(assert_recent_session_continuity(
            complete, "TEST", date(2026, 9, 4), len(idx), columns=["Close"]))
        missing = complete.drop(index=idx[-3])
        with self.assertRaisesRegex(ValueError, "missing 1 required XNYS session"):
            assert_recent_session_continuity(
                missing, "TEST", date(2026, 9, 4), len(idx), columns=["Close"])
        missing_earliest = complete.drop(index=idx[0])
        with self.assertRaisesRegex(ValueError, idx[0].date().isoformat()):
            assert_recent_session_continuity(
                missing_earliest, "TEST", date(2026, 9, 4), len(idx),
                columns=["Close"])

    def test_basket_continuity_requires_every_ticker_since_baseline(self):
        cal = bot.xnyse_calendar()
        sessions = cal.sessions_in_range("2026-09-01", "2026-09-04")
        idx = pd.DatetimeIndex(sessions).tz_localize(None)
        basket = pd.DataFrame({"A": 1.0, "B": 2.0}, index=idx)
        basket.loc[idx[1], "B"] = np.nan
        with self.assertRaisesRegex(ValueError, "missing 1 required XNYS session"):
            assert_recent_session_continuity(
                basket, "basket", date(2026, 9, 4), 1,
                columns=["A", "B"], start_session=date(2026, 9, 1))

    def test_missing_daily_close_is_filled_from_validated_regular_close(self):
        df = pd.DataFrame(
            {"Close": [122.0]}, index=pd.to_datetime(["2026-09-03"]))
        with patch.object(bot, "fetch_yahoo_regular_close", return_value=123.45):
            out = ensure_session_close(df, "SPY", date(2026, 9, 4))
        self.assertEqual(float(out.loc[pd.Timestamp("2026-09-04"), "Close"]), 123.45)


class MainSchedulingIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cal = bot.xnyse_calendar()
        sessions = cal.sessions_in_range("2024-01-01", "2026-09-04")[-500:]
        cls.idx = pd.DatetimeIndex(sessions).tz_localize(None)
        cls.spy = pd.DataFrame(
            {"Close": np.linspace(100.0, 150.0, len(cls.idx))}, index=cls.idx)
        cls.qqq = pd.DataFrame(
            {"Close": np.linspace(200.0, 275.0, len(cls.idx))}, index=cls.idx)
        cls.nasdaq = pd.DataFrame(
            {"NASDAQCOM": np.linspace(10000.0, 15000.0, len(cls.idx))}, index=cls.idx)
        cls.unrate = pd.DataFrame(
            {"UNRATE": [4.0, 4.0, 4.0, 4.0]},
            index=pd.to_datetime(["2026-05-01", "2026-06-01", "2026-07-01", "2026-08-01"]),
        )
        cls.cfg = {
            "year": 2026,
            "baseline_date": "2026-01-02",
            "holdings": [{"ticker": "TEST", "weight": 1.0, "baseline_close": 100.0}],
        }
        basket_idx = cls.idx[cls.idx >= pd.Timestamp("2026-01-02")]
        cls.basket = pd.DataFrame(
            {"TEST": np.linspace(100.0, 110.0, len(basket_idx))}, index=basket_idx)
        cls.run_time = pytz.timezone("America/New_York").localize(
            datetime(2026, 9, 4, 16, 30))

    def run_main(self, *, prev=None, qqq=None, final=False, basket_error=None,
                 force_partial=False):
        sent = Mock()
        saved = Mock()
        env = {
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "test-chat",
            "FRED_API_KEY": "test-key",
            "MANUAL_RUN": "true" if force_partial else "false",
            "FINAL_ATTEMPT": "true" if final else "false",
            "FORCE_REPEAT": "false",
            "FORCE_PARTIAL_DATA": "true" if force_partial else "false",
        }
        basket_fetch = Mock(side_effect=basket_error) if basket_error else Mock(return_value=self.basket)
        with patch.dict(bot.os.environ, env, clear=False), \
                patch.object(bot, "load_state", return_value=prev or {}), \
                patch.object(bot, "save_state", saved), \
                patch.object(bot, "send_telegram", sent), \
                patch.object(bot, "fetch_etf", side_effect=lambda ticker: self.spy if ticker == "SPY" else (qqq if qqq is not None else self.qqq)), \
                patch.object(bot, "fetch_etf_yfinance", side_effect=lambda ticker: self.spy if ticker == "SPY" else (qqq if qqq is not None else self.qqq)), \
                patch.object(bot, "fetch_nasdaqcom", return_value=self.nasdaq), \
                patch.object(bot, "fetch_nasdaqcom_yfinance", return_value=self.nasdaq), \
                patch.object(bot, "ensure_session_close", side_effect=lambda df, ticker, session_date, column="Close", **kwargs: df), \
                patch.object(bot, "ensure_basket_session_closes", side_effect=lambda df, tickers, session_date: df), \
                patch.object(bot, "fetch_unrate", return_value=self.unrate), \
                patch.object(bot, "load_basket_config", return_value=(self.cfg, None)), \
                patch.object(bot, "fetch_basket_closes", basket_fetch):
            bot.main(current_et=self.run_time)
        return sent, saved

    def test_complete_inputs_send_once_and_stamp_session(self):
        sent, saved = self.run_main()
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(saved.call_count, 1)
        state = saved.call_args.args[0]
        self.assertEqual(state["_meta"]["last_reported_market_date"], "2026-09-04")
        self.assertEqual(set(state["_meta"]["source_dates"].values()), {"2026-09-04"})

    def test_duplicate_session_skips_before_fetch_or_send(self):
        prev = {"_meta": {"last_reported_market_date": "2026-09-04"}}
        sent, saved = self.run_main(prev=prev)
        self.assertEqual(sent.call_count, 0)
        self.assertEqual(saved.call_count, 0)

    def test_incomplete_early_source_defers_without_mutation(self):
        stale_qqq = self.qqq.iloc[:-1]
        sent, saved = self.run_main(qqq=stale_qqq, final=False)
        self.assertEqual(sent.call_count, 0)
        self.assertEqual(saved.call_count, 0)

    def test_incomplete_final_source_fails_closed(self):
        stale_qqq = self.qqq.iloc[:-1]
        with self.assertRaisesRegex(RuntimeError, "QQQ"):
            self.run_main(qqq=stale_qqq, final=True)

    def test_ratchet_fetch_exception_defers_without_mutation(self):
        sent, saved = self.run_main(basket_error=RuntimeError("basket unavailable"))
        self.assertEqual(sent.call_count, 0)
        self.assertEqual(saved.call_count, 0)

    def test_forced_incomplete_manual_report_never_saves_state(self):
        sent, saved = self.run_main(qqq=self.qqq.iloc[:-1], force_partial=True)
        self.assertEqual(sent.call_count, 1)
        self.assertIn("FORCED INCOMPLETE-DATA DIAGNOSTIC", sent.call_args.args[2])
        self.assertEqual(saved.call_count, 0)


class WorkflowContractTests(unittest.TestCase):
    def test_fixed_eastern_retries_concurrency_and_failure_order(self):
        workflow = Path(".github/workflows/daily.yml").read_text()
        self.assertIn("cron: '20,40 17 * * 1-5'", workflow)
        self.assertIn("cron: '10 18 * * 1-5'", workflow)
        self.assertEqual(workflow.count('timezone: "America/New_York"'), 2)
        self.assertIn("group: daily-spy-sma-telegram", workflow)
        self.assertLess(
            workflow.index("Commit updated state.json"),
            workflow.index("Notify on workflow or state-persistence failure"),
        )

    def test_exchange_calendar_dependency_is_pinned(self):
        requirements = Path("requirements.txt").read_text().splitlines()
        self.assertIn("exchange-calendars==4.13.2", requirements)
        self.assertIn("yfinance==1.7.0", requirements)


if __name__ == "__main__":
    unittest.main()
