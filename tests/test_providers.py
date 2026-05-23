import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from kronos_mvp.providers import AkShareDailyProvider, BaoStockDailyProvider, ProviderError, _list_symbols_from_akshare, _list_symbols_from_baostock, infer_a_share_market, list_a_share_symbols


class ProviderSymbolListTests(unittest.TestCase):
    @patch.dict(os.environ, {"DATA_PROVIDERS": "akshare,baostock"}, clear=False)
    @patch("kronos_mvp.providers._list_symbols_from_baostock", return_value=["sh.600519", "sz.000001", "sz.200001", "bj.920001"])
    @patch("kronos_mvp.providers._list_symbols_from_akshare", side_effect=ProviderError("offline"))
    def test_list_a_share_symbols_uses_provider_fallback_and_filters_non_a_shares(self, _akshare, _baostock):
        self.assertEqual(list_a_share_symbols(), ["000001", "600519", "920001"])

    @patch.dict(os.environ, {"DATA_PROVIDERS": "akshare"}, clear=False)
    @patch("kronos_mvp.providers._list_symbols_from_akshare", return_value=["600519", "000001", "920001"])
    def test_list_a_share_symbols_filters_by_market(self, _akshare):
        self.assertEqual(list_a_share_symbols(market="sh"), ["600519"])
        self.assertEqual(list_a_share_symbols(market="sz"), ["000001"])
        self.assertEqual(list_a_share_symbols(market="bj"), ["920001"])

    def test_akshare_symbol_lookup_uses_market_specific_loader_when_available(self):
        fake_ak = SimpleNamespace(
            stock_info_bj_name_code=lambda: pd.DataFrame({"证券代码": ["920001", "430001"]}),
            stock_info_a_code_name=lambda: pd.DataFrame({"证券代码": ["600519"]}),
        )

        with patch.dict("sys.modules", {"akshare": fake_ak}):
            symbols = _list_symbols_from_akshare(market="bj")

        self.assertEqual(symbols, ["920001", "430001"])

    def test_infer_a_share_market_maps_supported_exchanges(self):
        self.assertEqual(infer_a_share_market("600519"), "sh")
        self.assertEqual(infer_a_share_market("000001"), "sz")
        self.assertEqual(infer_a_share_market("920001"), "bj")

    @patch("kronos_mvp.providers._latest_a_share_trading_day", return_value=date(2026, 5, 22))
    def test_baostock_symbol_lookup_uses_latest_trading_day(self, _latest_day):
        class FakeLoginResult:
            error_code = "0"
            error_msg = ""

        class FakeQueryResult:
            error_code = "0"
            error_msg = ""

            def __init__(self):
                self.rows = [["sh.600519"], ["sz.000001"]]
                self.index = -1

            def next(self):
                self.index += 1
                return self.index < len(self.rows)

            def get_row_data(self):
                return self.rows[self.index]

        class FakeBaoStock:
            def __init__(self):
                self.requested_day = None

            def login(self):
                return FakeLoginResult()

            def logout(self):
                return None

            def query_all_stock(self, day=None):
                self.requested_day = day
                return FakeQueryResult()

        fake_bs = FakeBaoStock()
        with patch.dict("sys.modules", {"baostock": fake_bs}):
            symbols = _list_symbols_from_baostock()

        self.assertEqual(fake_bs.requested_day, "2026-05-22")
        self.assertEqual(symbols, ["sh.600519", "sz.000001"])


class ProviderDailyFetchTests(unittest.TestCase):
    def test_akshare_bj_daily_fetch_falls_back_to_unadjusted_history(self):
        calls = []

        def stock_zh_a_hist(symbol, period, start_date, end_date, adjust):
            calls.append(adjust)
            if adjust == "":
                return pd.DataFrame(
                    {
                        "日期": ["2026-05-22"],
                        "开盘": [10.0],
                        "最高": [10.5],
                        "最低": [9.8],
                        "收盘": [10.2],
                        "成交量": [1000],
                        "成交额": [10200],
                    }
                )
            return pd.DataFrame()

        fake_ak = SimpleNamespace(stock_zh_a_hist=stock_zh_a_hist)

        with patch.dict("sys.modules", {"akshare": fake_ak}):
            result = AkShareDailyProvider().fetch_daily("920001", start_date=date(2026, 5, 1))

        self.assertEqual(calls, [""])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].close, 10.2)

    def test_baostock_daily_fetch_rejects_bj_symbols(self):
        fake_bs = SimpleNamespace(login=lambda: None)

        with patch.dict("sys.modules", {"baostock": fake_bs}):
            with self.assertRaisesRegex(ProviderError, "does not support BJ"):
                BaoStockDailyProvider().fetch_daily("920001")

    def test_baostock_daily_fetch_reuses_login_session(self):
        class FakeLoginResult:
            error_code = "0"
            error_msg = ""

        class FakeQueryResult:
            error_code = "0"
            error_msg = ""

            def __init__(self):
                self.rows = [["2026-05-22", "10", "11", "9", "10.5", "1000", "10500"]]
                self.index = -1

            def next(self):
                self.index += 1
                return self.index < len(self.rows)

            def get_row_data(self):
                return self.rows[self.index]

        class FakeBaoStock:
            def __init__(self):
                self.login_calls = 0
                self.logout_calls = 0
                self.query_calls = 0

            def login(self):
                self.login_calls += 1
                return FakeLoginResult()

            def logout(self):
                self.logout_calls += 1

            def query_history_k_data_plus(self, *args, **kwargs):
                self.query_calls += 1
                return FakeQueryResult()

        fake_bs = FakeBaoStock()
        with patch.dict("sys.modules", {"baostock": fake_bs}):
            provider = BaoStockDailyProvider()
            provider.fetch_daily("600519")
            provider.fetch_daily("600000")
            provider._logout()

        self.assertEqual(fake_bs.login_calls, 1)
        self.assertEqual(fake_bs.query_calls, 2)
        self.assertEqual(fake_bs.logout_calls, 1)


if __name__ == "__main__":
    unittest.main()