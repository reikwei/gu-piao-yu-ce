import os
import unittest
from unittest.mock import patch

from kronos_mvp.providers import ProviderError, infer_a_share_market, list_a_share_symbols


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

    def test_infer_a_share_market_maps_supported_exchanges(self):
        self.assertEqual(infer_a_share_market("600519"), "sh")
        self.assertEqual(infer_a_share_market("000001"), "sz")
        self.assertEqual(infer_a_share_market("920001"), "bj")


if __name__ == "__main__":
    unittest.main()