import os
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from kronos_mvp.providers import ProviderError, _list_symbols_from_akshare, _list_symbols_from_baostock, infer_a_share_market, list_a_share_symbols


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


if __name__ == "__main__":
    unittest.main()