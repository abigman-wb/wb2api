"""per-key 并发上限：迁移 / CRUD / allow / middleware。"""

import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from admin import gateway_limits
from admin.db import Base, get_db
from admin.models import ApiKey
from admin.routers import keys
from admin.security import require_admin


def _memory_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class MigrationTests(unittest.TestCase):
    def test_max_concurrent_column_exists(self):
        engine, _ = _memory_db()
        cols = [c["name"] for c in inspect(engine).get_columns("api_keys")]
        self.assertIn("max_concurrent", cols)
        self.assertIn("rate_per_minute", cols)
        engine.dispose()

    def test_existing_key_table_gets_nullable_rate_column_idempotently(self):
        from sqlalchemy import text

        from admin import db

        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE api_keys (id INTEGER PRIMARY KEY, max_concurrent INTEGER)"))
            conn.execute(text("INSERT INTO api_keys (id, max_concurrent) VALUES (1, 8)"))
        with patch.object(db, "engine", engine):
            db._ensure_column("api_keys", "rate_per_minute", "INT", "NULL")
            db._ensure_column("api_keys", "rate_per_minute", "INT", "NULL")
        with engine.connect() as conn:
            self.assertEqual(tuple(conn.execute(text("SELECT * FROM api_keys")).one()), (1, 8, None))
        engine.dispose()


class CrudTests(unittest.TestCase):
    def setUp(self):
        self.engine, Session = _memory_db()
        self.db = Session()

    def tearDown(self):
        self.db.close()

    def _client(self):
        app = FastAPI()
        app.include_router(keys.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[get_db] = lambda: self.db
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    def test_create_patch_max_concurrent(self):
        async def run():
            async with self._client() as c:
                r = await c.post("/api/keys", json={"name": "a", "max_concurrent": 5})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["max_concurrent"], 5)
                kid = r.json()["id"]

                r = await c.post("/api/keys", json={"name": "b", "max_concurrent": 0})
                self.assertEqual(r.json()["max_concurrent"], 0)

                r = await c.post("/api/keys", json={"name": "c"})
                self.assertIsNone(r.json()["max_concurrent"])

                r = await c.patch(f"/api/keys/{kid}", json={"max_concurrent": 0})
                self.assertEqual(r.status_code, 200)
                rows = (await c.get("/api/keys")).json()["items"]
                by_id = {x["id"]: x for x in rows}
                self.assertEqual(by_id[kid]["max_concurrent"], 0)

                r = await c.patch(f"/api/keys/{kid}", json={"max_concurrent": None})
                self.assertEqual(r.status_code, 200)
                rows = (await c.get("/api/keys")).json()["items"]
                by_id = {x["id"]: x for x in rows}
                self.assertIsNone(by_id[kid]["max_concurrent"])

        asyncio.run(run())

    def test_rate_is_saved_independently_and_null_restores_global(self):
        async def run():
            async with self._client() as c:
                response = await c.post(
                    "/api/keys", json={"name": "two-agents", "max_concurrent": 8, "rate_per_minute": 120}
                )
                self.assertEqual(response.status_code, 200)
                kid = response.json()["id"]
                self.assertEqual(response.json()["rate_per_minute"], 120)
                for rate in (0, None, 90):
                    response = await c.patch(f"/api/keys/{kid}", json={"rate_per_minute": rate})
                    self.assertEqual(response.status_code, 200)
                    row = next(k for k in (await c.get("/api/keys")).json()["items"] if k["id"] == kid)
                    self.assertEqual(row["rate_per_minute"], rate)
                    self.assertEqual(row["max_concurrent"], 8)

        asyncio.run(run())


class AllowTests(unittest.TestCase):
    def setUp(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    def tearDown(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    def test_per_key_one_slot(self):
        with patch.object(gateway_limits, "_limits", return_value=(0, 4)):
            a1 = gateway_limits.allow("k1", rate=0, concurrent=1)
            self.assertFalse(isinstance(a1, tuple))
            denied = gateway_limits.allow("k1", rate=0, concurrent=1)
            self.assertIsInstance(denied, tuple)
            self.assertEqual(denied[1], 1)
            gateway_limits.release(a1)
            ok = gateway_limits.allow("k1", rate=0, concurrent=1)
            self.assertFalse(isinstance(ok, tuple))
            gateway_limits.release(ok)

    def test_unlimited_zero(self):
        with patch.object(gateway_limits, "_limits", return_value=(0, 1)):
            holds = []
            for _ in range(8):
                r = gateway_limits.allow("k0", rate=0, concurrent=0)
                self.assertFalse(isinstance(r, tuple), r)
                holds.append(r)
            for h in holds:
                gateway_limits.release(h)

    def test_none_follows_global(self):
        with patch.object(gateway_limits, "_limits", return_value=(0, 1)):
            a1 = gateway_limits.allow("kN")  # concurrent=None → 全局 1
            self.assertFalse(isinstance(a1, tuple))
            denied = gateway_limits.allow("kN")
            self.assertIsInstance(denied, tuple)
            gateway_limits.release(a1)

    def test_rate_survives_release_and_concurrency_denial_does_not_spend_rate(self):
        a = gateway_limits.allow("shared", rate=2, concurrent=1)
        denied = gateway_limits.allow("shared", rate=2, concurrent=1)
        self.assertEqual(denied.reason, "concurrent_requests")
        self.assertEqual((denied.current, denied.limit), (1, 1))
        gateway_limits.release(a)
        b = gateway_limits.allow("shared", rate=2, concurrent=1)
        gateway_limits.release(b)
        denied = gateway_limits.allow("shared", rate=2, concurrent=8)
        self.assertEqual(denied.reason, "requests_per_minute")
        self.assertEqual((denied.current, denied.limit), (2, 2))
        # A key can disable RPM while preserving its concurrency protection.
        a = gateway_limits.allow("shared", rate=0, concurrent=1)
        self.assertIsInstance(a, gateway_limits.Admission)
        self.assertEqual(gateway_limits.allow("shared", rate=0, concurrent=1).reason, "concurrent_requests")
        gateway_limits.release(a)

    def test_redis_denials_preserve_the_same_reason_and_counts(self):
        from unittest.mock import MagicMock

        client = MagicMock()
        with patch.object(gateway_limits, "_redis_client", return_value=client):
            for result, expected in (([12, 1, 60], "requests_per_minute"), ([1, 2, 4], "concurrent_requests")):
                client.eval.return_value = result
                denied = gateway_limits.allow("shared", rate=60, concurrent=4)
                self.assertEqual(denied.reason, expected)
                self.assertEqual(denied[1], result[0])
                self.assertEqual(denied.current, result[2])

    def test_key_admission_params(self):
        engine, Session = _memory_db()
        session = Session()
        k = ApiKey(key_hash="h1", key_prefix="sk-test", max_concurrent=0)
        session.add(k)
        session.commit()
        kid = k.id
        session.close()

        def fake_get_key_row(db, token):
            if token == "t-unlimited":
                s = Session()
                row = s.query(ApiKey).get(kid)
                return row
            if token == "t-null":
                s = Session()
                row = s.query(ApiKey).filter(ApiKey.key_hash == "h1").first()
                # simulate max_concurrent=None
                row.max_concurrent = None
                s.commit()
                return row
            return None

        with (
            patch("admin.security.get_key_row", side_effect=fake_get_key_row),
            patch.object(gateway_limits, "_limits", return_value=(60, 4)),
        ):
            # monkeypatch import inside function: patch admin.security.get_key_row is enough
            key_id, rate, conc = gateway_limits._key_admission_params("t-unlimited")
            self.assertEqual(key_id, str(kid))
            self.assertEqual(rate, 60)
            self.assertEqual(conc, 0)

    def test_key_rate_override_is_resolved_separately_from_concurrency(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        row = SimpleNamespace(id=9, status="active", max_concurrent=8, rate_per_minute=None)
        with (
            patch("admin.db.SessionLocal", return_value=MagicMock()),
            patch("admin.security.get_key_row", return_value=row),
            patch.object(gateway_limits, "_limits", return_value=(60, 4)),
        ):
            for rate, expected in ((None, 60), (0, 0), (120, 120)):
                row.rate_per_minute = rate
                self.assertEqual(gateway_limits._key_admission_params("offline"), ("9", expected, 8))


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    def tearDown(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    async def test_429_when_per_key_exhausted(self):
        async def app(scope, receive, send):
            from starlette.responses import PlainTextResponse

            await PlainTextResponse("ok")(scope, receive, send)

        mw = gateway_limits.GatewayAdmissionMiddleware(app)
        calls = {"n": 0}

        def fake_params(token):
            calls["n"] += 1
            return "k9", 0, 1

        def fake_allow(key, rate=None, concurrent=None):
            if calls.get("held"):
                return None, 2
            calls["held"] = True
            return gateway_limits.Admission(key)

        async def run_one():
            scope = {
                "type": "http",
                "path": "/v1/messages",
                "headers": [(b"x-api-key", b"tok")],
                "method": "POST",
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            sent = []

            async def send(message):
                sent.append(message)

            return scope, receive, send, sent

        with (
            patch.object(gateway_limits, "_key_admission_params", side_effect=fake_params),
            patch.object(gateway_limits, "allow", side_effect=fake_allow),
            patch.object(gateway_limits, "release"),
        ):
            scope, receive, send, sent = await run_one()
            await mw(scope, receive, send)
            # first request succeeds (held=False → Admission)
            self.assertTrue(any(m.get("type") == "http.response.start" and m["status"] == 200 for m in sent))

            calls["held"] = True  # force deny
            scope, receive, send, sent = await run_one()
            await mw(scope, receive, send)
            starts = [m for m in sent if m.get("type") == "http.response.start"]
            self.assertEqual(starts[0]["status"], 429)

    async def test_real_two_agent_contention_and_release_on_failure(self):
        import json

        started, finish = asyncio.Event(), asyncio.Event()

        async def app(scope, receive, send):
            started.set()
            await finish.wait()
            raise RuntimeError("upstream failed")

        mw = gateway_limits.GatewayAdmissionMiddleware(app)
        scope = {"type": "http", "path": "/v1/messages", "method": "POST", "headers": [(b"x-api-key", b"offline")]}
        sent = []

        async def send(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b""}

        with patch.object(gateway_limits, "_key_admission_params", return_value=("shared", 0, 1)):
            first = asyncio.create_task(mw(scope, receive, send))
            await started.wait()
            await mw(scope, receive, send)
            self.assertEqual(sent[0]["status"], 429)
            error = json.loads(sent[1]["body"])["error"]
            self.assertEqual(error["code"], "concurrent_requests")
            self.assertIn("并发", error["message"])
            self.assertEqual(error["limit"], 1)
            finish.set()
            with self.assertRaises(RuntimeError):
                await first
        self.assertFalse(gateway_limits.snapshot()["active"])

    async def test_model_polling_does_not_consume_generation_quota(self):
        from starlette.responses import PlainTextResponse

        async def app(scope, receive, send):
            await PlainTextResponse("models")(scope, receive, send)

        async def send(message):
            pass

        async def receive():
            return {"type": "http.request", "body": b""}

        with patch.object(gateway_limits, "_key_admission_params") as params:
            await gateway_limits.GatewayAdmissionMiddleware(app)(
                {"type": "http", "method": "GET", "path": "/v1/models"}, receive, send
            )
        params.assert_not_called()

    async def test_many_dialogues_share_one_key_limit_instead_of_multiplying_it(self):
        from starlette.requests import Request

        entered, finish, rejected = asyncio.Event(), asyncio.Event(), asyncio.Event()
        running = peak = denials = 0
        app = FastAPI()
        app.add_middleware(gateway_limits.GatewayAdmissionMiddleware)

        @app.post("/v1/chat/completions")
        async def generate(request: Request):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            if running == 4:
                entered.set()
            try:
                await finish.wait()
                return {"conversation_id": (await request.json())["conversation_id"]}
            finally:
                running -= 1

        with patch.object(gateway_limits, "_key_admission_params", return_value=("shared", 0, 4)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline") as client:

                async def call(index):
                    nonlocal denials
                    response = await client.post(
                        "/v1/chat/completions",
                        headers={"x-api-key": "same-key"},
                        json={"conversation_id": f"chat-{index // 2}", "call": index % 2},
                    )
                    if response.status_code == 429:
                        denials += 1
                        self.assertEqual(response.json()["error"]["code"], "concurrent_requests")
                        if denials == 12:
                            rejected.set()
                    return response

                tasks = [asyncio.create_task(call(i)) for i in range(16)]
                try:
                    await asyncio.wait_for(entered.wait(), 3)
                    await asyncio.wait_for(rejected.wait(), 3)
                    self.assertEqual(peak, 4)
                    self.assertEqual(gateway_limits.snapshot()["active"], {"shared": 4})
                finally:
                    finish.set()
                    responses = await asyncio.gather(*tasks, return_exceptions=True)
                self.assertEqual(sum(isinstance(r, httpx.Response) and r.status_code == 200 for r in responses), 4)
                self.assertEqual(denials, 12)
                self.assertFalse(gateway_limits.snapshot()["active"])


class ConversationIsolationTests(unittest.TestCase):
    def headers(self, conversation=None, first="task", user_id=None):
        from admin.routers.proxy import _session_headers

        body = {
            "messages": [
                {"role": "user", "content": first},
                {"role": "assistant", "content": "working"},
                {"role": "user", "content": "continue"},
            ]
        }
        if conversation is not None:
            body["conversation_id"] = conversation
        if user_id is not None:
            body["metadata"] = {"user_id": user_id}
        return _session_headers(body, "")

    def test_identical_turns_in_different_explicit_conversations_have_distinct_roots(self):
        a, b = self.headers("chat-a"), self.headers("chat-b")
        self.assertNotEqual(a["X-Root-Request-ID"], b["X-Root-Request-ID"])
        self.assertEqual(a["X-Conversation-ID"], "chat-a")
        self.assertEqual(b["X-Conversation-ID"], "chat-b")

    def test_fallback_conversations_include_their_first_user_message(self):
        a, b = self.headers(first="task-a"), self.headers(first="task-b")
        self.assertNotEqual(a["X-Root-Request-ID"], b["X-Root-Request-ID"])

    def test_same_conversation_turn_remains_stable_across_attempts(self):
        a, b = self.headers("chat-a"), self.headers("chat-a")
        self.assertEqual(a["X-Root-Request-ID"], b["X-Root-Request-ID"])
        self.assertEqual(a["X-Conversation-Request-ID"], b["X-Conversation-Request-ID"])
        self.assertNotEqual(a["X-Request-ID"], b["X-Request-ID"])

    def test_user_id_without_conversation_id_does_not_merge_independent_requests(self):
        a, b = self.headers(user_id="one-user"), self.headers(user_id="one-user")
        self.assertNotEqual(a["X-Trace-ID"], b["X-Trace-ID"])
        self.assertNotIn("X-Root-Request-ID", a)


if __name__ == "__main__":
    unittest.main()
