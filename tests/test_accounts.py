import tempfile
import unittest
from pathlib import Path

from kronos_mvp.accounts import AccountError, AccountStore, annual_is_active, yuan_to_cents
from kronos_mvp.payments import sign_params


class AccountStoreTests(unittest.TestCase):
    def test_paid_balance_order_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "app.db")
            user = store.create_user("alice", "secret123")
            order = store.create_recharge_order(user["id"], yuan_to_cents("10"), "balance", "alipay")

            store.apply_paid_order(order["out_trade_no"], "saila-1", yuan_to_cents("10"), "{}")
            first_user = store.get_user(user["id"])
            store.apply_paid_order(order["out_trade_no"], "saila-1", yuan_to_cents("10"), "{}")
            second_user = store.get_user(user["id"])

        self.assertEqual(first_user["balance_cents"], 1000)
        self.assertEqual(second_user["balance_cents"], 1000)

    def test_convert_balance_to_annual(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "app.db")
            user = store.create_user("alice", "secret123")
            order = store.create_recharge_order(user["id"], yuan_to_cents("20"), "balance", "alipay")
            store.apply_paid_order(order["out_trade_no"], "saila-1", yuan_to_cents("20"), "{}")

            updated = store.convert_balance_to_annual(user["id"])

        self.assertEqual(updated["balance_cents"], 0)
        self.assertTrue(annual_is_active(updated["annual_until"]))

    def test_authorize_prediction_requires_recharge_after_free_credits_are_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / "app.db")
            user = store.create_user("alice", "secret123")

            for _ in range(int(user["free_credits_remaining"])):
                usage = store.authorize_prediction(user["id"], "600519")
                store.mark_prediction_succeeded(int(usage["id"]))

            with self.assertRaises(AccountError) as context:
                store.authorize_prediction(user["id"], "600519")

        self.assertEqual(context.exception.status_code, 402)
        self.assertEqual(context.exception.detail, "你的免费次数已用完，请到账户中心充值。")


class SailaPayTests(unittest.TestCase):
    def test_sign_params_excludes_empty_and_sign_fields(self):
        params = {
            "pid": "1001",
            "name": "VIP会员",
            "money": "1.00",
            "empty": "",
            "sign": "old",
            "sign_type": "MD5",
        }

        sign = sign_params(params, "secret")

        self.assertEqual(sign, sign_params({"money": "1.00", "name": "VIP会员", "pid": "1001"}, "secret"))


if __name__ == "__main__":
    unittest.main()
