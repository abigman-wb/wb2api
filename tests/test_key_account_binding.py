"""Offline ownership regressions with real SQLite and mocked upstream traffic."""
import asyncio
import importlib
import json
import sys
import types
import unittest
from contextlib import ExitStack, asynccontextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from admin import db as database, model_catalog, pool, upstream_http
from admin.account_binding import binding_owners, scope_accounts
from admin.db import Base, get_db
from admin.models import Account, ApiKey, AccountModelCooldown, ModelConfig, ModelGroup, ModelGroupItem
from admin.routers import keys
from admin.security import hash_key, require_admin

class _DelegatingBackend(types.ModuleType):
    """替身只覆盖需要打桩的三个属性，其余一律转发给真实 admin.backend。

    否则这个假模块会一直被 proxy.backend 引用，后续用例（如 mobile_latency 的
    timing 用例）调用 backend.stable_device_id() 时会报 AttributeError。
    """

    def __getattr__(self, name):
        return getattr(_real_backend, name)


_real_backend = importlib.import_module("admin.backend")
fake_backend = _DelegatingBackend("admin.backend")
fake_backend.HTTP_LIMITS = httpx.Limits()
fake_backend.AccountSession = type("AccountSession", (), {})
fake_backend.CredentialManager = type("CredentialManager", (), {})
with patch.dict(sys.modules, {"admin.backend": fake_backend}):
    proxy = importlib.import_module("admin.routers.proxy")
    accounts = importlib.import_module("admin.routers.accounts")


class Fixture:
    def setUp(self):
        # 1.14.0 起，routing_policy / upstream_dispatch / security.recheck_key 会直接用
        # admin.db.SessionLocal 读库（不经过 proxy 的 patch）。所以这里不再另建内存库，
        # 而是复用 tests/__init__.py 通过 ADMIN_DATABASE_URL 指定的全局隔离 SQLite，
        # 并在每个用例前清表，保证任意代码路径看到的是同一份数据。
        self.engine = database.engine
        self.factory = database.SessionLocal
        self._reset_schema()
        self.db = self.factory()
        self.a = Account(name="private", uid="private", auth_json='{"test":"private"}', balance_remain=100)
        self.b = Account(name="public", uid="public", auth_json='{"test":"public"}', balance_remain=100)
        self.db.add_all([self.a, self.b, ModelConfig(model_id="free", credit_multiplier=0),
                         ModelConfig(model_id="other", credit_multiplier=1)])
        self.db.commit()
        self.private = ApiKey(name="private", key_hash=hash_key("private-key"), account_id=self.a.id, unlimited=1)
        self.public = ApiKey(name="public", key_hash=hash_key("public-key"), unlimited=1)
        self.db.add_all([self.private, self.public])
        self.db.commit()
        self.stack = ExitStack()
        catalog = self.stack.enter_context(patch.object(proxy.model_catalog, "get_models", return_value=[{"id":"free"}]))
        self.catalog = catalog
        self.stack.enter_context(patch.object(proxy, "SessionLocal", self.factory))
        self.stack.enter_context(patch.object(proxy, "_record_usage", lambda *a, **kw: None))
        self.stack.enter_context(patch.object(proxy, "_log_chat_row", lambda *a, **kw: 1))
        self.stack.enter_context(patch.object(proxy, "_upstream_extra_headers", lambda *a, **kw: {}))
        self.stack.enter_context(patch.object(proxy.backend, "stable_device_id", lambda *a: "test", create=True))

    def _reset_schema(self):
        """每个用例从干净 schema 开始。

        有的用例会故意 DROP TABLE api_keys 造一张旧表来验证迁移（见
        test_management_http_contract 附近），共用全局库后它会影响后续用例，
        所以这里 drop_all + create_all + init_db 重建，保证互相隔离。
        """
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        database.init_db()

    def tearDown(self):
        self.stack.close()
        self.db.close()


class OwnershipTests(Fixture, unittest.TestCase):
    def test_public_private_and_wrong_owner(self):
        self.assertEqual(proxy._select_account(self.db, mark_picked=False).id, self.b.id)
        self.assertEqual(proxy._select_account(self.db, account_id=self.a.id, key_id=self.private.id).id, self.a.id)
        self.assertIsNone(proxy._select_account(self.db, account_id=self.a.id, key_id=self.public.id))
        self.assertIsNone(proxy._select_account(self.db, account_id=self.a.id, key_id=self.private.id, exclude_ids={self.a.id}))

    def test_fallback_cannot_cross_boundary(self):
        for a in (self.a, self.b):
            a.cool_kind = "upstream_internal"
            a.cool_until = datetime.utcnow() + timedelta(seconds=30)
        self.db.commit()
        self.assertEqual(proxy._select_account(self.db).id, self.b.id)
        self.assertEqual(proxy._select_account(self.db, account_id=self.a.id, key_id=self.private.id).id, self.a.id)
        self.b.status = "disabled"
        self.db.commit()
        self.assertIsNone(proxy._select_account(self.db))

    def test_sticky_cannot_cross_boundary(self):
        with patch.object(proxy.POOL.sticky, "resolve", side_effect=lambda key, uids: "private" if "private" in uids else None):
            self.assertEqual(proxy._select_account(self.db, sticky_key="session").id, self.b.id)

    def test_duplicate_uid_reserved_and_conflicts(self):
        dup = Account(uid=self.a.uid, auth_json="{}", balance_remain=999)
        self.db.add(dup)
        self.db.commit()
        self.assertEqual([a.id for a in scope_accounts(self.db.query(Account)).all()], [self.b.id])
        self.assertEqual(binding_owners(self.db)[dup.id].id, self.private.id)
        with self.assertRaises(HTTPException) as err:
            keys.create_key(keys.KeyIn(account_id=dup.id), db=self.db)
        self.assertEqual(err.exception.status_code, 409)

    def test_revocation_keeps_reservation_and_unbind_releases(self):
        keys.patch_key(self.private.id, {"status":"revoked"}, db=self.db)
        self.assertEqual(proxy._select_account(self.db).id, self.b.id)
        self.assertIsNone(proxy._select_account(self.db, account_id=self.a.id, key_id=self.private.id))
        keys.patch_key(self.private.id, {"account_id":0}, db=self.db)
        self.assertEqual({a.id for a in scope_accounts(self.db.query(Account)).all()}, {self.a.id, self.b.id})

    def test_delete_key_releases_and_delete_account_is_guarded(self):
        with self.assertRaises(HTTPException) as err:
            accounts.delete_account(self.a.id, db=self.db)
        self.assertEqual(err.exception.status_code, 409)
        keys.delete_key(self.private.id, db=self.db)
        self.assertIn(self.a.id, [a.id for a in scope_accounts(self.db.query(Account)).all()])
        accounts.delete_account(self.a.id, db=self.db)

    def test_bound_account_unavailable_does_not_fall_back(self):
        for state in ("empty", "disabled", "breaker", "missing"):
            with self.subTest(state=state):
                self.a.status = "disabled" if state == "disabled" else "active"
                self.a.balance_remain = 0 if state == "empty" else 100
                self.a.breaker_until = datetime.utcnow() + timedelta(hours=1) if state == "breaker" else None
                self.db.commit()
                aid = 9876 if state == "missing" else self.a.id
                self.assertIsNone(proxy._select_account(self.db, model="other", account_id=aid, key_id=self.private.id))

    def test_entitlement_group_and_global_intersection(self):
        self.assertEqual(proxy._key_allowed_models(self.db, self.private), {"free"})
        self.assertIsNone(proxy._pick_best_model(self.db, "other", proxy._key_allowed_models(self.db, self.private)))
        group = ModelGroup(name="restricted")
        self.db.add(group)
        self.db.flush()
        self.db.add(ModelGroupItem(group_id=group.id, model_id="other"))
        self.private.group_id = group.id
        self.db.commit()
        self.assertEqual(proxy._key_allowed_models(self.db, self.private), set())
        self.private.group_id = 0
        self.db.query(ModelConfig).filter_by(model_id="free").update({"enabled":0})
        self.db.commit()
        self.assertEqual(proxy._key_allowed_models(self.db, self.private), set())

    def test_catalog_failure_is_closed(self):
        self.catalog.side_effect = RuntimeError("offline")
        with self.assertRaises(HTTPException) as err:
            proxy._key_allowed_models(self.db, self.private)
        self.assertEqual(err.exception.status_code, 503)

    def test_database_constraint_and_old_schema_migration(self):
        self.db.add(ApiKey(key_hash="race", account_id=self.a.id))
        with self.assertRaises(IntegrityError): self.db.commit()
        self.db.rollback()
        self.db.close()
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE api_keys"))
            conn.execute(text("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, key_hash VARCHAR(128), group_id INT DEFAULT 0)"))
        with patch.object(database, "engine", self.engine):
            self.assertTrue(database.init_db())
            self.assertTrue(database.init_db())
        self.assertIn("account_id", [c["name"] for c in inspect(self.engine).get_columns("api_keys")])
        self.assertTrue(any(i["unique"] for i in inspect(self.engine).get_indexes("api_keys") if i["name"] == "ux_api_keys_account"))

    def test_management_http_contract(self):
        app = FastAPI()
        app.include_router(keys.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[get_db] = lambda: self.db
        async def exercise():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.post("/api/keys", json={"account_id":self.a.id})).status_code, 409)
                self.assertEqual((await client.post("/api/keys", json={"account_id":9999})).status_code, 400)
                self.assertEqual((await client.patch(f"/api/keys/{self.public.id}", json={"account_id":"bad"})).status_code, 400)
                created = await client.post("/api/keys", json={"account_id":self.b.id, "unlimited":True})
                self.assertEqual(created.status_code, 200)
                self.assertEqual(created.json()["account_id"], self.b.id)
                rows = (await client.get("/api/keys")).json()["items"]
                self.assertEqual(rows[0]["account_name"], "public")
                self.assertEqual((await client.patch(f'/api/keys/{created.json()["id"]}', json={"account_id":None})).status_code, 200)
        asyncio.run(exercise())

    def test_models_failure_retries_only_bound_account(self):
        session = MagicMock()
        session.updated_json.return_value = '{}'
        session.fetch_models.side_effect = RuntimeError("HTTP 500 upstream")
        session.fetch_model_busy.return_value = {"code":0, "data":[]}
        context = MagicMock()
        context.__enter__.return_value = session
        with patch.object(proxy.backend, "AccountSession", return_value=context):
            result = proxy.models(db=self.db, x_api_key="public-key", authorization=None)
            self.assertEqual(result.status_code, 502)
            self.db.expire_all()
            self.assertEqual(self.a.err_count, 0)
            session.fetch_models.side_effect = None
            session.fetch_models.return_value = [{"id":"free"}]
            result = proxy.models(db=self.db, x_api_key="private-key", authorization=None)
            self.assertEqual([m["id"] for m in result["data"]], ["free"])
            session.fetch_models.reset_mock()
            session.fetch_models.side_effect = RuntimeError("HTTP 500 upstream")
            result = proxy.models(db=self.db, x_api_key="private-key", authorization=None)
            self.assertEqual(result.status_code, 502)
            session.fetch_models.assert_called_once()
            self.assertIn('1 个账号', json.loads(result.body)["error"]["message"])

    def test_dedupe_preserves_reserved_record(self):
        older = Account(uid="duplicate", auth_json="{}", balance_remain=100)
        newer = Account(uid="duplicate", auth_json="{}", balance_remain=100)
        self.db.add_all([older, newer])
        self.db.commit()
        self.db.add(ApiKey(key_hash="duplicate-owner", account_id=newer.id))
        self.db.commit()
        accounts.dedupe_accounts(db=self.db)
        self.assertIsNotNone(self.db.get(Account, newer.id))
        self.assertNotIn(older.id, [a.id for a in self.db.query(Account).all()])


class ProtocolTests(Fixture, unittest.IsolatedAsyncioTestCase):
    async def test_all_protocols_streaming_and_nonstreaming_ownership(self):
        success = ('data: '+json.dumps({"id":"t", "choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":None}]})+'\n\n'
                   +'data: '+json.dumps({"id":"t", "choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":2,"completion_tokens":1,"total_tokens":3}})+'\n\n'
                   +'data: [DONE]\n\n')
        class Credential:
            def __init__(self, a): self.a = a
            def get_headers(self, extra=None): return {"test-account":str(self.a.id)}
            def updated_json(self): return self.a.auth_json
            def close(self): pass
        async def session(db, a): return Credential(a)
        async def backoff(n): pass
        original = httpx.AsyncClient
        for handler, protocol in [(proxy.chat_completions,"chat"), (proxy.responses_proxy,"responses"), (proxy.anthropic_messages,"messages")]:
            for streaming in (False, True):
                for token, expected in [("private-key",self.a.id), ("public-key",self.b.id)]:
                    for failure in (False, True):
                        with self.subTest(protocol=protocol, stream=streaming, key=token, failure=failure):
                            with self.factory() as reset:
                                for a in reset.query(Account):
                                    a.cool_until = a.breaker_until = a.degrade_until = None
                                    a.cool_kind = ""
                                reset.query(AccountModelCooldown).delete()
                                reset.commit()
                            calls = []
                            async def transport(req):
                                calls.append(int(req.headers["test-account"]))
                                body = 'data: {"code":6004,"msg":"daily limit"}\n\n' if failure else success
                                return httpx.Response(200,text=body,headers={"content-type":"text/event-stream"})
                            @asynccontextmanager
                            async def fake_client(request, timeout, limits, *, retry_budget=None, _t=transport):
                                # 1.14.0 把 AsyncClient 的构造收进了
                                # admin.upstream_http.client()，并且**无条件**塞入
                                # _BorrowedTransport（真实网络）。所以不能再 patch
                                # httpx.AsyncClient，只能在 client() 这一层替换。
                                async with original(transport=httpx.MockTransport(_t),
                                                    timeout=timeout, limits=limits) as conn:
                                    yield conn
                            payload = {"model":"free", "stream":streaming, "messages":[{"role":"user","content":"hi"}], "max_tokens":20}
                            if protocol == "responses": payload = {"model":"free","stream":streaming,"input":"hi"}
                            async def receive(): return {"type":"http.request","body":json.dumps(payload).encode(),"more_body":False}
                            request = Request({"type":"http","method":"POST","path":"/","headers":[],"client":("127.0.0.1",1)},receive)
                            with patch.object(proxy, "_account_session_async",session), patch.object(upstream_http,"client",fake_client), patch.object(proxy.pool,"rotate_backoff_async",backoff):
                                with self.factory() as db:
                                    result = await handler(request, db=db, x_api_key=token, authorization=None)
                                    from tests import collect_response
                                    output = (await collect_response(result)).decode()
                            self.assertEqual(calls,[expected])
                            if not failure: self.assertIn("hello",output)
                            self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_all_protocols_reject_other_account_models_before_upstream(self):
        for handler, protocol in [(proxy.chat_completions,"chat"), (proxy.responses_proxy,"responses"), (proxy.anthropic_messages,"messages")]:
            payload = {"model":"other","messages":[{"role":"user","content":"hi"}],"max_tokens":20}
            if protocol == "responses": payload["input"] = "hi"
            async def receive(): return {"type":"http.request","body":json.dumps(payload).encode(),"more_body":False}
            request = Request({"type":"http","method":"POST","path":"/","headers":[]},receive)
            with patch.object(proxy,"_account_session_async",side_effect=AssertionError("must not call upstream")):
                result = await handler(request,db=self.db,x_api_key="private-key",authorization=None)
            self.assertEqual(result.status_code,403)
            self.assertEqual(json.loads(result.body)["error"]["type"],"model_not_entitled")


if __name__ == "__main__":
    unittest.main()
