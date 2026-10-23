"""窗口预算的后台接口：读写、优先级、校验，以及选号是否真的按保存值生效。"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from admin.config import settings
from admin.db import SessionLocal, get_db, init_db
from admin.models import Account, AccountModelCooldown, ApiKey, SystemSetting, UsageLog
from admin.routers import pool_budget
from admin.routers.proxy import _select_account
from admin.security import require_admin


class PoolBudgetApiTests(unittest.TestCase):
    def setUp(self):
        init_db()
        self.db = SessionLocal()
        self._clear()
        self.app = FastAPI()
        self.app.include_router(pool_budget.router)
        self.app.dependency_overrides[require_admin] = lambda: True
        self.app.dependency_overrides[get_db] = lambda: self.db
        self.client = TestClient(self.app)

    def tearDown(self):
        self._clear()
        self.db.close()

    def _clear(self):
        # 其它套件可能留下模型级冷却行或同 uid 的在途租约；不清理会让这里的选号用例
        # 在多套件一起跑时偶发 None（单跑模块却是绿的）。
        self.db.query(UsageLog).delete()
        self.db.query(AccountModelCooldown).delete()
        self.db.query(ApiKey).filter(ApiKey.account_id.is_not(None)).delete(synchronize_session=False)
        self.db.query(Account).delete()
        self.db.query(SystemSetting).filter(SystemSetting.key == pool_budget.credit_budget.KEY).delete()
        self.db.commit()

    def _account(self, name, balance=900):
        acc = Account(name=name, uid=f"{name}-{id(self)}-{name}", auth_json="{}", status="active",
                      balance_total=balance, balance_remain=balance)
        self.db.add(acc)
        self.db.commit()
        self.db.refresh(acc)
        return acc

    def _spend(self, acc, credits, minutes_ago=10):
        self.db.add(UsageLog(api_key_id=1, account_id=acc.id, model="kimi-k3-1", credits=credits,
                             error_kind="success", created_at=datetime.utcnow() - timedelta(minutes=minutes_ago)))
        self.db.commit()

    def test_default_is_disabled(self):
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 0.0):
            data = self.client.get("/api/pool/window-budget").json()
        self.assertEqual(data["budget"], 0)
        self.assertFalse(data["enabled"])
        self.assertEqual(data["source"], "off")

    def test_env_value_is_used_when_nothing_saved(self):
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 90.0), \
                patch.object(settings, "WINDOW_CREDIT_HOURS", 4.0):
            data = self.client.get("/api/pool/window-budget").json()
        self.assertEqual(data["budget"], 90)
        self.assertEqual(data["hours"], 4)
        self.assertEqual(data["source"], "env")
        self.assertTrue(data["enabled"])

    def test_saved_value_wins_over_env(self):
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 90.0):
            r = self.client.put("/api/pool/window-budget", json={"budget": 210, "hours": 5})
            self.assertEqual(r.status_code, 200)
            data = self.client.get("/api/pool/window-budget").json()
        self.assertEqual(data["budget"], 210)
        self.assertEqual(data["source"], "saved")

    def test_delete_falls_back_to_env(self):
        self.client.put("/api/pool/window-budget", json={"budget": 210, "hours": 5})
        with patch.object(settings, "WINDOW_CREDIT_BUDGET", 90.0):
            data = self.client.delete("/api/pool/window-budget").json()
        self.assertEqual(data["budget"], 90)
        self.assertEqual(data["source"], "env")

    def test_rejects_out_of_range_values(self):
        self.assertEqual(self.client.put("/api/pool/window-budget",
                                         json={"budget": -1, "hours": 5}).status_code, 422)
        self.assertEqual(self.client.put("/api/pool/window-budget",
                                         json={"budget": 210, "hours": 30}).status_code, 422)
        self.assertEqual(self.client.put("/api/pool/window-budget",
                                         json={"budget": 200000, "hours": 5}).status_code, 422)

    def test_account_usage_is_reported(self):
        acc = self._account("哄哄")
        self._spend(acc, 105)
        self.client.put("/api/pool/window-budget", json={"budget": 210, "hours": 5})
        item = self.client.get("/api/pool/window-budget").json()["accounts"][0]
        self.assertEqual(item["name"], "哄哄")
        self.assertEqual(item["used"], 105)
        self.assertEqual(item["percent"], 50)
        self.assertFalse(item["over"])

    def test_over_budget_flag_is_set(self):
        acc = self._account("哈哈")
        self._spend(acc, 260)
        self.client.put("/api/pool/window-budget", json={"budget": 210, "hours": 5})
        item = self.client.get("/api/pool/window-budget").json()["accounts"][0]
        self.assertTrue(item["over"])

    def test_gate_uses_saved_value(self):
        over = self._account("over")
        ok = self._account("ok")
        self._spend(over, 250)
        self.client.put("/api/pool/window-budget", json={"budget": 210, "hours": 5})
        picked = _select_account(self.db, model="deepseek-v4.1-flash", mark_picked=False)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, ok.id)

    def test_gate_stops_when_budget_set_to_zero(self):
        a = self._account("a")
        self._spend(a, 250)
        self.client.put("/api/pool/window-budget", json={"budget": 0, "hours": 5})
        picked = _select_account(self.db, model="deepseek-v4.1-flash", mark_picked=False)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, a.id)


if __name__ == "__main__":
    unittest.main()
