"""窗口积分预算：选号前按 usage_logs 的滚动窗口剔除超预算账号。

背景（2026-10-23 线上事故）：主模型被换成 1.62 倍率的模型后，40 万 token 的
长会话在 35 分钟里打出 780 积分，三个号的上游「账号×5 小时窗口」额度被一次性
打爆，返回 6000 并冷却 4.5 小时，期间整池 503。本模块的预算闸门在撞墙前停手。
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from admin.config import settings
from admin.db import SessionLocal, init_db
from admin.models import Account, AccountModelCooldown, ApiKey, UsageLog
from admin.routers.proxy import (
    _filter_window_budget,
    _no_account_reason,
    _select_account,
    _window_credit_spend,
)


class WindowBudgetTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        self._clear()
        self.now = datetime.utcnow()

    def tearDown(self):
        self._clear()
        self.db.close()

    def _clear(self):
        self.db.query(UsageLog).delete()
        self.db.query(AccountModelCooldown).delete()
        self.db.query(ApiKey).filter(ApiKey.account_id.is_not(None)).delete(synchronize_session=False)
        self.db.query(Account).delete()
        self.db.commit()

    def _account(self, name, balance=1000):
        acc = Account(name=name, uid=f"{name}-{id(self)}-{name}", auth_json="{}", status="active",
                      balance_total=balance, balance_remain=balance)
        self.db.add(acc)
        self.db.commit()
        self.db.refresh(acc)
        return acc

    def _spend(self, acc, credits, minutes_ago=10, error_kind="success"):
        self.db.add(UsageLog(api_key_id=1, account_id=acc.id, model="kimi-k3-1",
                             credits=credits, error_kind=error_kind,
                             created_at=self.now - timedelta(minutes=minutes_ago)))
        self.db.commit()

    def test_disabled_by_default_keeps_every_account(self):
        a, b = self._account("a"), self._account("b")
        self._spend(a, 9999)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 0.0):
            kept = _filter_window_budget(self.db, [a, b], self.now)
        self.assertEqual([x.name for x in kept], ["a", "b"])

    def test_excludes_account_over_budget(self):
        a, b = self._account("a"), self._account("b")
        self._spend(a, 230)
        self._spend(b, 40)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            kept = _filter_window_budget(self.db, [a, b], self.now)
        self.assertEqual([x.name for x in kept], ["b"])

    def test_spend_exactly_at_budget_is_excluded(self):
        a = self._account("a")
        self._spend(a, 210)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual(_filter_window_budget(self.db, [a], self.now), [])

    def test_spend_just_below_budget_is_kept(self):
        a = self._account("a")
        self._spend(a, 209.99)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual([x.name for x in _filter_window_budget(self.db, [a], self.now)], ["a"])

    def test_usage_outside_window_does_not_count(self):
        a = self._account("a")
        self._spend(a, 500, minutes_ago=int(settings.WINDOW_CREDIT_HOURS * 60) + 10)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual([x.name for x in _filter_window_budget(self.db, [a], self.now)], ["a"])

    def test_usage_inside_window_counts(self):
        a = self._account("a")
        self._spend(a, 300, minutes_ago=int(settings.WINDOW_CREDIT_HOURS * 60) - 10)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual(_filter_window_budget(self.db, [a], self.now), [])

    def test_failed_requests_without_credits_count_as_zero(self):
        a = self._account("a")
        self._spend(a, None, error_kind="no_account")
        self._spend(a, None, error_kind="soft_rate")
        self.assertEqual(_window_credit_spend(self.db, [a.id], self.now).get(a.id, 0.0), 0.0)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertEqual([x.name for x in _filter_window_budget(self.db, [a], self.now)], ["a"])

    def test_window_hours_is_configurable(self):
        a = self._account("a")
        self._spend(a, 100, minutes_ago=70)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 50.0), \
                patch.object(settings, "WINDOW_CREDIT_HOURS", 1.0):
            # 70 分钟前的用量在 1 小时窗口之外 → 不计入，账号仍可用。
            self.assertEqual([x.name for x in _filter_window_budget(self.db, [a], self.now)], ["a"])
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 50.0), \
                patch.object(settings, "WINDOW_CREDIT_HOURS", 2.0):
            # 同一笔用量落在 2 小时窗口内 → 超预算被剔除。
            self.assertEqual(_filter_window_budget(self.db, [a], self.now), [])

    def test_select_account_skips_over_budget_account(self):
        over, ok = self._account("over"), self._account("ok")
        self._spend(over, 250)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            picked = _select_account(self.db, model="deepseek-v4.1-flash", mark_picked=False)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, ok.id)

    def test_select_account_returns_none_when_all_over_budget(self):
        a, b = self._account("a"), self._account("b")
        self._spend(a, 250)
        self._spend(b, 260)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            self.assertIsNone(_select_account(self.db, model="deepseek-v4.1-flash", mark_picked=False))

    def test_select_account_ignores_budget_when_disabled(self):
        a, b = self._account("a"), self._account("b")
        self._spend(a, 250)
        self._spend(b, 260)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 0.0):
            self.assertIsNotNone(_select_account(self.db, model="deepseek-v4.1-flash", mark_picked=False))

    def test_no_account_reason_explains_window_budget(self):
        a, b = self._account("a"), self._account("b")
        self._spend(a, 260)
        self._spend(b, 240)
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 210.0):
            reason = _no_account_reason(self.db)
        self.assertIn("窗口预算", reason)
        self.assertIn("不需要人工处理", reason)


if __name__ == "__main__":
    unittest.main()
