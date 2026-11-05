"""预算闸门的对外信号：错误类型、文案、Retry-After。

线上现象（2026-11-01 冻结）：本地预算把整池清空后，客户端只看到一句
「号池里没有可用账号」，HTTP 503 + Retry-After 3 —— 会话于是每 3 秒重试一次，
日志里查不出原因（上游根本没被调用）。这里锁住三件事：
  1. 分类可区分（window_budget ≠ no_account）；
  2. 文案带每号用量与恢复时间；
  3. Retry-After 是真实恢复秒数，不是 3。
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from admin.config import settings
from admin.db import SessionLocal, init_db
from admin.models import Account, ApiKey, UsageLog
from admin.routers import proxy
from admin.routers.proxy import (
    _filter_window_budget,
    _no_account_kind,
    _no_account_reason,
    _no_account_status,
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
        self.assertEqual(_no_account_kind(), "no_account")
        self.assertEqual(_no_account_status(), 503)
        self.assertEqual(_retry_after_value(), "3")


if __name__ == "__main__":
    unittest.main()
