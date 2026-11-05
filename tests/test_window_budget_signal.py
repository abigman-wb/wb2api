"""预算闸门的对外信号：错误类型、文案、Retry-After。

线上现象（2026-11-01 冻结）：本地预算把整池清空后，客户端只看到一句
「号池里没有可用账号」，HTTP 503 + Retry-After 3 —— 会话于是每 3 秒重试一次，
日志里查不出原因（上游根本没被调用）。这里锁住三件事：
  1. 分类可区分（window_budget ≠ no_account）；
  2. 文案带每号用量与恢复时间；
  3. Retry-After 是真实恢复秒数，不是 3。
"""

import asyncio
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from admin.config import settings
from admin.db import SessionLocal, init_db
from admin.models import Account, ApiKey, UsageLog
from admin.routers import proxy
from admin.routers.proxy import (
    _filter_window_budget,
    _no_account_code,
    _no_account_kind,
    _no_account_reason,
    _no_account_status,
    _no_account_type,
    _retry_after_value,
)


class WindowBudgetSignalTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        self.now = datetime.utcnow()
        proxy._WINDOW_BLOCK.clear()
        self.db.query(UsageLog).delete()
        self.db.query(ApiKey).filter(ApiKey.account_id.is_not(None)).delete(synchronize_session=False)
        self.db.query(Account).delete()
        self.db.commit()

    def tearDown(self):
        proxy._WINDOW_BLOCK.clear()
        self.db.query(UsageLog).delete()
        self.db.query(Account).delete()
        self.db.commit()
        self.db.close()

    def _account(self, name):
        acc = Account(name=name, uid=f"{name}-{id(self)}", auth_json="{}", status="active",
                      balance_total=1000, balance_remain=1000)
        self.db.add(acc)
        self.db.commit()
        self.db.refresh(acc)
        return acc

    def _spend(self, acc, credits, minutes_ago=10):
        self.db.add(UsageLog(api_key_id=1, account_id=acc.id, model="kimi-k3.1-pro",
                             credits=credits, error_kind="success",
                             created_at=self.now - timedelta(minutes=minutes_ago)))
        self.db.commit()

    def test_block_is_reported_as_window_budget(self):
        a = self._account("哄哄")
        self._spend(a, 250)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual(_filter_window_budget(self.db, [a], self.now), [])
            self.assertEqual(_no_account_kind(), "window_budget")
            self.assertEqual(_no_account_status(), 429)
            self.assertEqual(_no_account_code(), "pool_window_budget_exhausted")
            self.assertEqual(_no_account_type(), "rate_limit_error")
            self.assertGreater(int(_retry_after_value()), 3)
            reason = _no_account_reason(self.db)
        self.assertIn("预算已用满", reason)
        self.assertIn("哄哄", reason)
        self.assertIn("210", reason)

    def test_retry_after_follows_oldest_usage_rolloff(self):
        a = self._account("a")
        self._spend(a, 100, minutes_ago=240)   # 1 小时后滚出窗口
        self._spend(a, 150, minutes_ago=10)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0), \
                patch.object(settings, "WINDOW_CREDIT_HOURS", 5.0):
            _filter_window_budget(self.db, [a], self.now)
            wait = int(_retry_after_value())
        # 需要释放 40 分 → 最老那笔（100 分）滚出即可，约 1 小时。
        self.assertGreater(wait, 3000)
        self.assertLess(wait, 3900)

    def test_no_block_keeps_generic_semantics(self):
        a = self._account("a")
        self._spend(a, 10)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual([x.name for x in _filter_window_budget(self.db, [a], self.now)], ["a"])
        self.assertFalse(self.db.info.get("budget_blocked"))
        self.assertEqual(_no_account_kind(), "no_account")
        self.assertEqual(_no_account_status(), 503)
        self.assertEqual(_retry_after_value(), "3")

    def test_filter_flags_budget_block_for_callers(self):
        """整池被预算清空时要留痕，否则等号逻辑会继续排队 90 秒。"""
        a = self._account("a")
        self._spend(a, 250)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual(_filter_window_budget(self.db, [a], self.now), [])
        self.assertTrue(self.db.info.get("budget_blocked"))

    def test_preflight_blocks_before_stream_starts(self):
        """入口预检：整池超预算时必须先判死，不能先开 SSE 再报错。"""
        a = self._account("a")
        b = self._account("b")
        self._spend(a, 250)
        self._spend(b, 260)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertTrue(proxy._window_budget_blocked(self.db))
        self.assertEqual(_no_account_status(), 429)

    def test_preflight_passes_when_one_account_left(self):
        a = self._account("a")
        b = self._account("b")
        self._spend(a, 250)
        self._spend(b, 10)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertFalse(proxy._window_budget_blocked(self.db))

    def test_wait_does_not_queue_when_budget_empties_pool(self):
        """预算拦截必须立刻返回（<2s），而不是等满 GATEWAY_QUEUE_TIMEOUT。

        线上表现是「请求挂 60-90 秒后回 503」，客户端还以为是被限速重试。
        """
        a = self._account("a")
        self._spend(a, 250)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            started = time.monotonic()
            got = asyncio.run(proxy._select_account_with_wait(
                self.db, model="kimi-k3.1-pro", min_balance=1))
            elapsed = time.monotonic() - started
        self.assertIsNone(got)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
