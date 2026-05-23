import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from kronos_mvp.models import Candle
from kronos_mvp.predictors import KronosPredictor, next_trading_days


class FakeUpstreamKronosPredictor:
    def __init__(self):
        self.calls = 0

    def predict(self, df, x_timestamp, y_timestamp, pred_len, T, top_p, sample_count):
        self.calls += 1
        last_close = float(df["close"].iloc[-1])
        return df.__class__(
            {
                "open": [last_close + self.calls + i for i in range(pred_len)],
                "high": [last_close + self.calls + i + 1 for i in range(pred_len)],
                "low": [last_close + self.calls + i - 1 for i in range(pred_len)],
                "close": [last_close + self.calls + i + 0.5 for i in range(pred_len)],
            }
        )


class FakeTradingCalendar:
    def __init__(self, sessions: list[str]):
        self.sessions = pd.DatetimeIndex(pd.Timestamp(session) for session in sessions)

    def sessions_in_range(self, start, end):
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end)
        return self.sessions[(self.sessions >= start_ts) & (self.sessions <= end_ts)]


class KronosPredictorTests(unittest.TestCase):
    def test_predict_returns_requested_kronos_paths_and_future_trading_days(self):
        candles = [
            Candle(date=date(2026, 5, 18), open=10, high=11, low=9, close=10, volume=100, amount=1000),
            Candle(date=date(2026, 5, 19), open=10, high=12, low=10, close=11, volume=110, amount=1210),
            Candle(date=date(2026, 5, 20), open=11, high=13, low=10, close=12, volume=120, amount=1440),
            Candle(date=date(2026, 5, 21), open=12, high=14, low=11, close=13, volume=130, amount=1690),
            Candle(date=date(2026, 5, 22), open=13, high=15, low=12, close=14, volume=140, amount=1960),
        ]
        upstream = FakeUpstreamKronosPredictor()
        predictor = KronosPredictor(upstream_predictor=upstream)
        fake_calendar = FakeTradingCalendar(["2026-05-25", "2026-05-26", "2026-05-27"])

        with patch("kronos_mvp.predictors._get_a_share_calendar", return_value=fake_calendar):
            result = predictor.predict("600519", candles, horizon=3, paths=2)

        self.assertEqual(result.symbol, "600519")
        self.assertEqual(result.backend, "kronos")
        self.assertEqual(len(result.paths), 2)
        self.assertEqual([p.date.isoformat() for p in result.paths[0].points], ["2026-05-25", "2026-05-26", "2026-05-27"])
        self.assertEqual(result.paths[0].points[0].close, 15.5)
        self.assertEqual(result.paths[1].points[0].close, 16.5)
        self.assertEqual(upstream.calls, 2)

    def test_next_trading_days_uses_a_share_calendar_not_weekdays_only(self):
        fake_calendar = FakeTradingCalendar(["2026-10-09", "2026-10-12", "2026-10-13"])

        with patch("kronos_mvp.predictors._get_a_share_calendar", return_value=fake_calendar):
            result = next_trading_days(date(2026, 10, 1), count=3)

        self.assertEqual([day.isoformat() for day in result], ["2026-10-09", "2026-10-12", "2026-10-13"])


if __name__ == "__main__":
    unittest.main()
