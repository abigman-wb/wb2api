"""真实积分回写（_fetch_real_credits）回归：修复 NameError 后的端到端行为。

覆盖四件事：
1. 估算落库后回写线程能真正校正 UsageLog.credits / ApiKey.credit_used（此前
   `AccountSession` 未导入，函数必然 NameError，回写从未生效）；
2. 幂等：已回写的日志不会被二次校正；
3. 一条上游用量记录只能认领一次：同账号同模型 ±5min 的兄弟日志不会被重复校正；
4. 候选过滤：model/client 不匹配的上游记录不参与校正。
"""

import datetime
import importlib
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import admin.backend as real_backend
from admin import db as database
from admin.db import Base, init_db
from admin.models import Account, ApiKey, ModelConfig, UsageLog
from admin.security import hash_key

proxy = importlib.import_module("admin.routers.proxy")


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class _FakeSession:
    """替身 AccountSession：按实例携带的 payload 返回用量。"""

    payloads = []

    def __init__(self, auth_json: str):
        self.auth_json = auth_json
        self.closed = False

    def fetch_request_usage(self, start_time, end_time, page_num=1, page_size=10):
        return {"data": {"data": _FakeSession.payloads.pop(0) if _FakeSession.payloads else []}}

    def close(self):
        self.closed = True


class RealCreditWritebackTest(unittest.TestCase):
    def setUp(self):
        self.engine = database.engine
        self.factory = database.SessionLocal
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        init_db()
        self.db = self.factory()
        self.acc = Account(name="acc", uid="acc", auth_json='{"test": 1}', balance_remain=100)
        self.db.add(self.acc)
        self.db.add(ModelConfig(model_id="m1", credit_multiplier=1))
        self.db.commit()
        self.key = ApiKey(name="k", key_hash=hash_key("k-key"), unlimited=1, credit_used=10)
        self.db.add(self.key)
        self.db.commit()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(proxy, "SessionLocal", self.factory))
        self.stack.enter_context(patch.object(real_backend, "AccountSession", _FakeSession))
        self.stack.enter_context(patch.object(proxy.time, "sleep", lambda *_: None))
        self.stack.enter_context(
            patch.object(proxy.settings, "UPSTREAM_CLIENT_NAME", "WorkBuddy"))

    def tearDown(self):
        self.stack.close()
        self.db.close()

    def _make_log(self, credits=2.0, minutes_ago=0, model="m1"):
        log = UsageLog(
            api_key_id=self.key.id, account_id=self.acc.id, model=model,
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
            credits=credits,
            created_at=datetime.datetime.utcnow() - datetime.timedelta(minutes=minutes_ago),
            error_kind="",
        )
        self.db.add(log)
        self.db.commit()
        return log

    @staticmethod
    def _row(model, request_time, credit, client="WorkBuddy-web"):
        return {"model": model, "client": client, "requestTime": _fmt(request_time),
                "credit": str(credit)}

    def test_writeback_corrects_credits_and_key(self):
        log = self._make_log(credits=2.0)
        t = log.created_at.replace(microsecond=0)
        _FakeSession.payloads = [[self._row("m1", t + datetime.timedelta(seconds=30), 1.5)]]
        proxy._fetch_real_credits(log.id, self.acc.id, "{}", "m1", 2.0, t)
        self.db.expire_all()
        log = self.db.query(UsageLog).get(log.id)
        self.assertAlmostEqual(log.credits, 1.5)
        self.assertAlmostEqual(self.db.query(ApiKey).get(self.key.id).credit_used, 9.5)
        diag = json.loads(log.diagnostic_json or "{}")
        self.assertEqual(diag["real_credit"]["real"], 1.5)

    def test_idempotent_second_run_skipped(self):
        log = self._make_log(credits=2.0)
        t = log.created_at.replace(microsecond=0)
        _FakeSession.payloads = [[
            self._row("m1", t + datetime.timedelta(seconds=30), 1.5),
            self._row("m1", t + datetime.timedelta(seconds=90), 9.9),
        ]]
        proxy._fetch_real_credits(log.id, self.acc.id, "{}", "m1", 2.0, t)
        self.db.expire_all()
        self.assertAlmostEqual(self.db.query(UsageLog).get(log.id).credits, 1.5)
        # 第二次触发（重复派发/重放）必须被幂等标记拦住，即使上游给了新候选
        _FakeSession.payloads = [[
            self._row("m1", t + datetime.timedelta(seconds=90), 9.9),
        ]]
        proxy._fetch_real_credits(log.id, self.acc.id, "{}", "m1", 2.0, t)
        self.db.expire_all()
        self.assertAlmostEqual(self.db.query(UsageLog).get(log.id).credits, 1.5)

    def test_sibling_claim_prevents_double_correction(self):
        log1 = self._make_log(credits=2.0, minutes_ago=0)
        log2 = self._make_log(credits=2.0, minutes_ago=1)
        claimed_time = log1.created_at.replace(microsecond=0) + datetime.timedelta(seconds=30)
        _FakeSession.payloads = [[self._row("m1", claimed_time, 1.5)]]
        proxy._fetch_real_credits(log1.id, self.acc.id, "{}", "m1", 2.0,
                                  log1.created_at.replace(microsecond=0))
        # 第二条：唯一候选已被 log1 认领，不得再被套用
        _FakeSession.payloads = [[self._row("m1", claimed_time, 1.5)]]
        proxy._fetch_real_credits(log2.id, self.acc.id, "{}", "m1", 2.0,
                                  log2.created_at.replace(microsecond=0))
        self.db.expire_all()
        self.assertAlmostEqual(self.db.query(UsageLog).get(log1.id).credits, 1.5)
        self.assertAlmostEqual(self.db.query(UsageLog).get(log2.id).credits, 2.0)

    def test_upstream_match_requires_client_and_model(self):
        log = self._make_log()
        t = log.created_at.replace(microsecond=0)
        _FakeSession.payloads = [[
            self._row("other", t + datetime.timedelta(seconds=30), 0.1),
            self._row("m1", t + datetime.timedelta(seconds=30), 0.2, client="OtherClient"),
        ]]
        proxy._fetch_real_credits(log.id, self.acc.id, "{}", "m1", 2.0, t)
        self.db.expire_all()
        self.assertAlmostEqual(self.db.query(UsageLog).get(log.id).credits, 2.0)


if __name__ == "__main__":
    unittest.main()
