from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterator


QUERY_PRICE_CENTS = 20
FREE_CREDITS_ON_SIGNUP = 10
ANNUAL_PRICE_CENTS = 2000
MIN_RECHARGE_CENTS = 100
MAX_RECHARGE_CENTS = 20000
SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60

_PASSWORD_SCHEME = "pbkdf2_sha256"
_PASSWORD_ROUNDS = 260_000
_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.\u4e00-\u9fff-]{3,32}$")
_CONTACT_MAX_LENGTH = 80


class AccountError(Exception):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class AccountStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    is_banned INTEGER NOT NULL DEFAULT 0,
                    free_credits_remaining INTEGER NOT NULL DEFAULT 10,
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    annual_until TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_login_at TEXT
                );

                CREATE TABLE IF NOT EXISTS user_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    revoked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS recharge_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    out_trade_no TEXT NOT NULL UNIQUE,
                    trade_no TEXT,
                    order_type TEXT NOT NULL,
                    pay_type TEXT,
                    amount_cents INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    raw_notify TEXT,
                    created_at TEXT NOT NULL,
                    paid_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prediction_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    symbol TEXT NOT NULL,
                    charge_type TEXT NOT NULL,
                    charge_cents INTEGER NOT NULL DEFAULT 0,
                    free_credits_charged INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS wallet_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    type TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL DEFAULT 0,
                    free_credits_delta INTEGER NOT NULL DEFAULT 0,
                    balance_after_cents INTEGER NOT NULL,
                    free_credits_after INTEGER NOT NULL,
                    related_order_id INTEGER,
                    related_usage_id INTEGER,
                    operator_user_id INTEGER,
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS admin_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operator_user_id INTEGER NOT NULL REFERENCES users(id),
                    target_user_id INTEGER,
                    action TEXT NOT NULL,
                    before_json TEXT,
                    after_json TEXT,
                    ip TEXT,
                    user_agent TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON user_sessions(token_hash);
                CREATE INDEX IF NOT EXISTS idx_orders_user ON recharge_orders(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_usage_user ON prediction_usage(user_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_ledger_user ON wallet_ledger(user_id, created_at);
                """
            )
            self._ensure_user_columns(conn)

    def _ensure_user_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "contact" not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN contact TEXT")

    def bootstrap_admin(self, username: str, password: str | None) -> None:
        if not password:
            return
        normalized = normalize_username(username or "admin")
        now = utc_now_text()
        with self._transaction() as conn:
            admin_count = conn.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'admin'").fetchone()["total"]
            if admin_count:
                return
            existing = conn.execute("SELECT id FROM users WHERE username = ?", (normalized,)).fetchone()
            password_hash = hash_password(password)
            if existing:
                conn.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, role = 'admin', is_banned = 0, updated_at = ?
                    WHERE id = ?
                    """,
                    (password_hash, now, existing["id"]),
                )
                return
            conn.execute(
                """
                INSERT INTO users(username, password_hash, role, free_credits_remaining, balance_cents, created_at, updated_at)
                VALUES (?, ?, 'admin', 0, 0, ?, ?)
                """,
                (normalized, password_hash, now, now),
            )

    def create_user(self, username: str, password: str, role: str = "user", contact: str | None = None) -> sqlite3.Row:
        normalized = normalize_username(username)
        normalized_contact = normalize_contact(contact)
        validate_username(normalized)
        validate_password(password)
        validate_contact(normalized_contact)
        if role not in {"user", "admin"}:
            raise AccountError("无效用户角色。")
        now = utc_now_text()
        free_credits = 0 if role == "admin" else FREE_CREDITS_ON_SIGNUP
        try:
            with self._transaction() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users(username, password_hash, role, free_credits_remaining, contact, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (normalized, hash_password(password), role, free_credits, normalized_contact, now, now),
                )
                return self._get_user_by_id(conn, int(cursor.lastrowid))
        except sqlite3.IntegrityError as exc:
            raise AccountError("用户名已存在。", status_code=409) from exc

    def authenticate_user(self, username: str, password: str) -> sqlite3.Row | None:
        normalized = normalize_username(username)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (normalized,)).fetchone()
            if row is None or not verify_password(password, row["password_hash"]):
                return None
            if int(row["is_banned"]):
                raise AccountError("账号已被封禁。", status_code=403)
            conn.execute("UPDATE users SET last_login_at = ?, updated_at = ? WHERE id = ?", (utc_now_text(), utc_now_text(), row["id"]))
            return self._get_user_by_id(conn, int(row["id"]))

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(32)
        now_dt = utc_now()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO user_sessions(user_id, token_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    user_id,
                    hash_token(token),
                    to_text(now_dt),
                    to_text(now_dt + timedelta(seconds=SESSION_MAX_AGE_SECONDS)),
                ),
            )
        return token

    def revoke_session(self, token: str) -> None:
        if not token:
            return
        with self._transaction() as conn:
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
                (utc_now_text(), hash_token(token)),
            )

    def get_user_by_session(self, token: str | None) -> sqlite3.Row | None:
        if not token:
            return None
        now = utc_now_text()
        with self._connection() as conn:
            return conn.execute(
                """
                SELECT users.*
                FROM user_sessions
                JOIN users ON users.id = user_sessions.user_id
                WHERE user_sessions.token_hash = ?
                  AND user_sessions.revoked_at IS NULL
                  AND user_sessions.expires_at > ?
                """,
                (hash_token(token), now),
            ).fetchone()

    def get_user(self, user_id: int) -> sqlite3.Row | None:
        with self._connection() as conn:
            return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    def list_users(self) -> list[dict[str, object]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM users ORDER BY id DESC LIMIT 500").fetchall()
        return [public_user(row) for row in rows]

    def list_orders(self) -> list[dict[str, object]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT recharge_orders.*, users.username
                FROM recharge_orders
                JOIN users ON users.id = recharge_orders.user_id
                ORDER BY recharge_orders.id DESC
                LIMIT 200
                """
            ).fetchall()
        return [dict_from_row(row) for row in rows]

    def list_usages(self) -> list[dict[str, object]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT prediction_usage.*, users.username
                FROM prediction_usage
                JOIN users ON users.id = prediction_usage.user_id
                ORDER BY prediction_usage.id DESC
                LIMIT 200
                """
            ).fetchall()
        return [dict_from_row(row) for row in rows]

    def change_password(self, user_id: int, current_password: str, new_password: str) -> sqlite3.Row:
        validate_password(new_password)
        now = utc_now_text()
        with self._transaction() as conn:
            user = self._get_user_by_id(conn, user_id)
            if int(user["is_banned"]):
                raise AccountError("账号已被封禁，不能修改密码。", status_code=403)
            if not verify_password(current_password, user["password_hash"]):
                raise AccountError("当前密码不正确。", status_code=401)
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(new_password), now, user_id),
            )
            return self._get_user_by_id(conn, user_id)

    def authorize_prediction(self, user_id: int, symbol: str) -> dict[str, object]:
        normalized_symbol = symbol.strip().lower()
        now = utc_now_text()
        with self._transaction() as conn:
            user = self._get_user_by_id(conn, user_id)
            if int(user["is_banned"]):
                raise AccountError("账号已被封禁，不能继续查询。", status_code=403)

            charge_type = "balance"
            charge_cents = 0
            free_credits_charged = 0
            if user["role"] == "admin":
                charge_type = "admin"
            elif annual_is_active(user["annual_until"]):
                charge_type = "annual"
            elif int(user["free_credits_remaining"]) > 0:
                charge_type = "free_credit"
                free_credits_charged = 1
                conn.execute(
                    """
                    UPDATE users
                    SET free_credits_remaining = free_credits_remaining - 1, updated_at = ?
                    WHERE id = ? AND free_credits_remaining > 0
                    """,
                    (now, user_id),
                )
            elif int(user["balance_cents"]) >= QUERY_PRICE_CENTS:
                charge_type = "balance"
                charge_cents = QUERY_PRICE_CENTS
                conn.execute(
                    """
                    UPDATE users
                    SET balance_cents = balance_cents - ?, updated_at = ?
                    WHERE id = ? AND balance_cents >= ?
                    """,
                    (QUERY_PRICE_CENTS, now, user_id, QUERY_PRICE_CENTS),
                )
            else:
                raise AccountError("你的免费次数已用完，请到账户中心充值。", status_code=402)

            cursor = conn.execute(
                """
                INSERT INTO prediction_usage(user_id, symbol, charge_type, charge_cents, free_credits_charged, status, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?)
                """,
                (user_id, normalized_symbol, charge_type, charge_cents, free_credits_charged, now),
            )
            usage_id = int(cursor.lastrowid)

            updated_user = self._get_user_by_id(conn, user_id)
            if charge_cents or free_credits_charged:
                conn.execute(
                    """
                    INSERT INTO wallet_ledger(
                        user_id, type, amount_cents, free_credits_delta, balance_after_cents,
                        free_credits_after, related_usage_id, note, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        "query_charge" if charge_cents else "free_credit_charge",
                        -charge_cents,
                        -free_credits_charged,
                        updated_user["balance_cents"],
                        updated_user["free_credits_remaining"],
                        usage_id,
                        f"预测查询 {normalized_symbol}",
                        now,
                    ),
                )
            return {
                "id": usage_id,
                "chargeType": charge_type,
                "chargeCents": charge_cents,
                "freeCreditsCharged": free_credits_charged,
            }

    def mark_prediction_succeeded(self, usage_id: int) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE prediction_usage SET status = 'succeeded', finished_at = ? WHERE id = ?",
                (utc_now_text(), usage_id),
            )

    def mark_prediction_failed(self, usage_id: int, reason: str) -> None:
        now = utc_now_text()
        with self._transaction() as conn:
            usage = conn.execute("SELECT * FROM prediction_usage WHERE id = ?", (usage_id,)).fetchone()
            if usage is None or usage["status"] != "pending":
                return
            conn.execute(
                """
                UPDATE prediction_usage
                SET status = 'refunded', error_message = ?, finished_at = ?
                WHERE id = ?
                """,
                (reason[:500], now, usage_id),
            )
            if usage["charge_type"] == "balance" and int(usage["charge_cents"]) > 0:
                conn.execute(
                    "UPDATE users SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                    (usage["charge_cents"], now, usage["user_id"]),
                )
                user = self._get_user_by_id(conn, int(usage["user_id"]))
                conn.execute(
                    """
                    INSERT INTO wallet_ledger(user_id, type, amount_cents, balance_after_cents, free_credits_after, related_usage_id, note, created_at)
                    VALUES (?, 'refund', ?, ?, ?, ?, ?, ?)
                    """,
                    (usage["user_id"], usage["charge_cents"], user["balance_cents"], user["free_credits_remaining"], usage_id, "预测失败自动退款", now),
                )
            elif usage["charge_type"] == "free_credit" and int(usage["free_credits_charged"]) > 0:
                conn.execute(
                    "UPDATE users SET free_credits_remaining = free_credits_remaining + ?, updated_at = ? WHERE id = ?",
                    (usage["free_credits_charged"], now, usage["user_id"]),
                )
                user = self._get_user_by_id(conn, int(usage["user_id"]))
                conn.execute(
                    """
                    INSERT INTO wallet_ledger(user_id, type, free_credits_delta, balance_after_cents, free_credits_after, related_usage_id, note, created_at)
                    VALUES (?, 'refund', ?, ?, ?, ?, ?, ?)
                    """,
                    (usage["user_id"], usage["free_credits_charged"], user["balance_cents"], user["free_credits_remaining"], usage_id, "预测失败返还免费次数", now),
                )

    def create_recharge_order(self, user_id: int, amount_cents: int, order_type: str, pay_type: str | None) -> sqlite3.Row:
        if order_type not in {"balance", "annual"}:
            raise AccountError("无效订单类型。")
        if pay_type and pay_type not in {"alipay"}:
            raise AccountError("无效支付方式。")
        if order_type == "annual" and amount_cents != ANNUAL_PRICE_CENTS:
            raise AccountError("包年套餐金额固定为 20 元。")
        if amount_cents < MIN_RECHARGE_CENTS or amount_cents > MAX_RECHARGE_CENTS:
            raise AccountError("充值金额必须在 1 元到 200 元之间。")
        out_trade_no = build_out_trade_no(user_id)
        now = utc_now_text()
        with self._transaction() as conn:
            self._get_user_by_id(conn, user_id)
            cursor = conn.execute(
                """
                INSERT INTO recharge_orders(user_id, out_trade_no, order_type, pay_type, amount_cents, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, out_trade_no, order_type, pay_type, amount_cents, now, now),
            )
            return conn.execute("SELECT * FROM recharge_orders WHERE id = ?", (int(cursor.lastrowid),)).fetchone()

    def apply_paid_order(self, out_trade_no: str, trade_no: str, amount_cents: int, raw_notify: str) -> sqlite3.Row:
        now_dt = utc_now()
        now = to_text(now_dt)
        with self._transaction() as conn:
            order = conn.execute("SELECT * FROM recharge_orders WHERE out_trade_no = ?", (out_trade_no,)).fetchone()
            if order is None:
                raise AccountError("订单不存在。", status_code=404)
            if int(order["amount_cents"]) != int(amount_cents):
                raise AccountError("支付金额与订单金额不一致。", status_code=400)
            if order["status"] == "paid":
                return order
            if order["status"] != "pending":
                raise AccountError("订单状态不允许入账。", status_code=409)

            user = self._get_user_by_id(conn, int(order["user_id"]))
            if order["order_type"] == "balance":
                conn.execute(
                    "UPDATE users SET balance_cents = balance_cents + ?, updated_at = ? WHERE id = ?",
                    (order["amount_cents"], now, order["user_id"]),
                )
            else:
                new_until = annual_extended_until(user["annual_until"], now_dt)
                conn.execute(
                    "UPDATE users SET annual_until = ?, updated_at = ? WHERE id = ?",
                    (to_text(new_until), now, order["user_id"]),
                )

            conn.execute(
                """
                UPDATE recharge_orders
                SET trade_no = ?, status = 'paid', raw_notify = ?, paid_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (trade_no, raw_notify, now, now, order["id"]),
            )
            updated_user = self._get_user_by_id(conn, int(order["user_id"]))
            conn.execute(
                """
                INSERT INTO wallet_ledger(user_id, type, amount_cents, balance_after_cents, free_credits_after, related_order_id, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["user_id"],
                    "recharge" if order["order_type"] == "balance" else "annual_recharge",
                    order["amount_cents"] if order["order_type"] == "balance" else 0,
                    updated_user["balance_cents"],
                    updated_user["free_credits_remaining"],
                    order["id"],
                    "塞拉支付入账",
                    now,
                ),
            )
            return conn.execute("SELECT * FROM recharge_orders WHERE id = ?", (order["id"],)).fetchone()

    def convert_balance_to_annual(self, user_id: int) -> sqlite3.Row:
        now_dt = utc_now()
        now = to_text(now_dt)
        with self._transaction() as conn:
            user = self._get_user_by_id(conn, user_id)
            if int(user["is_banned"]):
                raise AccountError("账号已被封禁，不能转换包年。", status_code=403)
            if int(user["balance_cents"]) < ANNUAL_PRICE_CENTS:
                raise AccountError("余额不足 20 元，不能转换包年。", status_code=402)
            new_until = annual_extended_until(user["annual_until"], now_dt)
            conn.execute(
                """
                UPDATE users
                SET balance_cents = balance_cents - ?, annual_until = ?, updated_at = ?
                WHERE id = ? AND balance_cents >= ?
                """,
                (ANNUAL_PRICE_CENTS, to_text(new_until), now, user_id, ANNUAL_PRICE_CENTS),
            )
            updated_user = self._get_user_by_id(conn, user_id)
            conn.execute(
                """
                INSERT INTO wallet_ledger(user_id, type, amount_cents, balance_after_cents, free_credits_after, note, created_at)
                VALUES (?, 'annual_convert', ?, ?, ?, ?, ?)
                """,
                (user_id, -ANNUAL_PRICE_CENTS, updated_user["balance_cents"], updated_user["free_credits_remaining"], "余额转换包年", now),
            )
            return updated_user

    def admin_adjust_balance(self, operator_id: int, user_id: int, delta_cents: int, note: str = "") -> sqlite3.Row:
        now = utc_now_text()
        with self._transaction() as conn:
            operator = self._get_user_by_id(conn, operator_id)
            require_admin_row(operator)
            before = self._get_user_by_id(conn, user_id)
            new_balance = max(0, int(before["balance_cents"]) + int(delta_cents))
            conn.execute("UPDATE users SET balance_cents = ?, updated_at = ? WHERE id = ?", (new_balance, now, user_id))
            after = self._get_user_by_id(conn, user_id)
            conn.execute(
                """
                INSERT INTO wallet_ledger(user_id, type, amount_cents, balance_after_cents, free_credits_after, operator_user_id, note, created_at)
                VALUES (?, 'admin_adjust', ?, ?, ?, ?, ?, ?)
                """,
                (user_id, new_balance - int(before["balance_cents"]), after["balance_cents"], after["free_credits_remaining"], operator_id, note, now),
            )
            self._write_audit(conn, operator_id, user_id, "adjust_balance", dict_from_row(before), dict_from_row(after))
            return after

    def admin_adjust_free_credits(self, operator_id: int, user_id: int, delta: int, note: str = "") -> sqlite3.Row:
        now = utc_now_text()
        with self._transaction() as conn:
            operator = self._get_user_by_id(conn, operator_id)
            require_admin_row(operator)
            before = self._get_user_by_id(conn, user_id)
            new_credits = max(0, int(before["free_credits_remaining"]) + int(delta))
            conn.execute("UPDATE users SET free_credits_remaining = ?, updated_at = ? WHERE id = ?", (new_credits, now, user_id))
            after = self._get_user_by_id(conn, user_id)
            conn.execute(
                """
                INSERT INTO wallet_ledger(user_id, type, free_credits_delta, balance_after_cents, free_credits_after, operator_user_id, note, created_at)
                VALUES (?, 'free_credit_adjust', ?, ?, ?, ?, ?, ?)
                """,
                (user_id, new_credits - int(before["free_credits_remaining"]), after["balance_cents"], after["free_credits_remaining"], operator_id, note, now),
            )
            self._write_audit(conn, operator_id, user_id, "adjust_free_credits", dict_from_row(before), dict_from_row(after))
            return after

    def admin_set_banned(self, operator_id: int, user_id: int, banned: bool) -> sqlite3.Row:
        now = utc_now_text()
        with self._transaction() as conn:
            operator = self._get_user_by_id(conn, operator_id)
            require_admin_row(operator)
            before = self._get_user_by_id(conn, user_id)
            conn.execute("UPDATE users SET is_banned = ?, updated_at = ? WHERE id = ?", (1 if banned else 0, now, user_id))
            after = self._get_user_by_id(conn, user_id)
            self._write_audit(conn, operator_id, user_id, "ban" if banned else "unban", dict_from_row(before), dict_from_row(after))
            return after

    def admin_set_annual_days(self, operator_id: int, user_id: int, days: int | None) -> sqlite3.Row:
        now_dt = utc_now()
        annual_until = None if days is None else to_text(now_dt + timedelta(days=max(0, int(days))))
        with self._transaction() as conn:
            operator = self._get_user_by_id(conn, operator_id)
            require_admin_row(operator)
            before = self._get_user_by_id(conn, user_id)
            conn.execute("UPDATE users SET annual_until = ?, updated_at = ? WHERE id = ?", (annual_until, to_text(now_dt), user_id))
            after = self._get_user_by_id(conn, user_id)
            self._write_audit(conn, operator_id, user_id, "set_annual", dict_from_row(before), dict_from_row(after))
            return after

    def admin_reset_password(self, operator_id: int, user_id: int, new_password: str) -> sqlite3.Row:
        validate_password(new_password)
        now = utc_now_text()
        with self._transaction() as conn:
            operator = self._get_user_by_id(conn, operator_id)
            require_admin_row(operator)
            before = self._get_user_by_id(conn, user_id)
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
                (hash_password(new_password), now, user_id),
            )
            conn.execute(
                "UPDATE user_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                (now, user_id),
            )
            after = self._get_user_by_id(conn, user_id)
            before_public = public_user(before)
            after_public = public_user(after)
            self._write_audit(conn, operator_id, user_id, "reset_password", before_public, after_public)
            return after

    def _get_user_by_id(self, conn: sqlite3.Connection, user_id: int) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            raise AccountError("用户不存在。", status_code=404)
        return row

    def _write_audit(
        self,
        conn: sqlite3.Connection,
        operator_user_id: int,
        target_user_id: int,
        action: str,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        import json

        conn.execute(
            """
            INSERT INTO admin_audit_logs(operator_user_id, target_user_id, action, before_json, after_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (operator_user_id, target_user_id, action, json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), utc_now_text()),
        )


def normalize_username(username: str) -> str:
    return username.strip()


def normalize_contact(contact: str | None) -> str | None:
    if contact is None:
        return None
    normalized = contact.strip()
    return normalized or None


def validate_username(username: str) -> None:
    if not _USERNAME_PATTERN.fullmatch(username):
        raise AccountError("用户名需为 3-32 位，可使用中文、字母、数字、下划线、点或短横线。")


def validate_password(password: str) -> None:
    if len(password) < 6:
        raise AccountError("密码至少需要 6 位。")


def validate_contact(contact: str | None) -> None:
    if contact and len(contact) > _CONTACT_MAX_LENGTH:
        raise AccountError("联系方式最多 80 个字符。")


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PASSWORD_ROUNDS).hex()
    return f"{_PASSWORD_SCHEME}${_PASSWORD_ROUNDS}${salt}${digest}"


def verify_password(password: str, password_hash: str) -> bool:
    try:
        scheme, rounds_text, salt, expected = password_hash.split("$", 3)
        if scheme != _PASSWORD_SCHEME:
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds_text)).hex()
    except Exception:
        return False
    return hmac.compare_digest(digest, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_text() -> str:
    return to_text(utc_now())


def to_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def annual_is_active(value: str | None) -> bool:
    parsed = parse_datetime(value)
    return bool(parsed and parsed > utc_now())


def annual_extended_until(value: str | None, now_dt: datetime) -> datetime:
    current_until = parse_datetime(value)
    start = current_until if current_until and current_until > now_dt else now_dt
    return start + timedelta(days=365)


def public_user(row: sqlite3.Row) -> dict[str, object]:
    balance_cents = int(row["balance_cents"])
    annual_until = row["annual_until"]
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "contact": row["contact"] or "",
        "role": row["role"],
        "isAdmin": row["role"] == "admin",
        "isBanned": bool(row["is_banned"]),
        "freeCreditsRemaining": int(row["free_credits_remaining"]),
        "balanceCents": balance_cents,
        "balanceYuan": cents_to_yuan(balance_cents),
        "annualUntil": annual_until,
        "annualActive": annual_is_active(annual_until),
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastLoginAt": row["last_login_at"],
    }


def dict_from_row(row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def require_admin_row(row: sqlite3.Row) -> None:
    if row["role"] != "admin":
        raise AccountError("需要管理员权限。", status_code=403)


def build_out_trade_no(user_id: int) -> str:
    timestamp = utc_now().strftime("%Y%m%d%H%M%S")
    return f"YP{timestamp}{user_id:06d}{secrets.token_hex(3)}"


def yuan_to_cents(value: str | int | float | Decimal) -> int:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise AccountError("金额格式不正确。") from exc
    return int(amount * 100)


def cents_to_yuan(cents: int) -> str:
    return f"{Decimal(int(cents)) / Decimal(100):.2f}"
