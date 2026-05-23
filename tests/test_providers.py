import os
import unittest
from unittest.mock import patch

from kronos_mvp.providers import ProviderError, list_a_share_symbols


class ProviderSymbolListTests(unittest.TestCase):
    @patch.dict(os.environ, {"DATA_PROVIDERS": "akshare,baostock"}, clear=False)
    @patch("kronos_mvp.providers._list_symbols_from_baostock", return_value=["sh.600519", "sz.000001", "sz.200001", "bj.920001"])
    @patch("kronos_mvp.providers._list_symbols_from_akshare", side_effect=ProviderError("offline"))
    def test_list_a_share_symbols_uses_provider_fallback_and_filters_non_a_shares(self, _akshare, _baostock):
        self.assertEqual(list_a_share_symbols(), ["000001", "600519", "920001"])


if __name__ == "__main__":
    unittest.main()