"""Exercise model discovery caching, connection reuse and client-visible timing."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import jwt
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

import converter
from admin import gateway_limits, mobile_stream, model_catalog, request_metrics, upstream_http
from admin.db import Base
from admin.models import ModelConfig
from admin.request_scheduling import SweepBudget
from admin.routers import proxy


class CatalogTests(unittest.TestCase):
    def setUp(self):
        model_catalog._cache.clear()

    def test_reuses_discovery_expires_and_isolates_credentials(self):
        fetch = MagicMock(return_value=[{"id": "paid", "credits": "x1.00"}, {"id": "free", "credits": "x0.00"}])
        with patch.object(model_catalog.time, "monotonic", return_value=10):
            self.assertEqual(model_catalog.auto_model("A", "backend", fetch), "free")
            self.assertEqual(model_catalog.auto_model("A", "backend", fetch), "free")
            self.assertEqual(fetch.call_count, 1)
            model_catalog.auto_model("B", "backend", fetch)
            model_catalog.auto_model("A", "different-backend", fetch)
            self.assertEqual(fetch.call_count, 3)
        with patch.object(model_catalog.time, "monotonic", return_value=311):
            model_catalog.auto_model("A", "backend", fetch)
            self.assertEqual(fetch.call_count, 4)

    def test_failure_is_not_cached_and_local_rules_override_discovery(self):
        fetch = MagicMock(side_effect=[RuntimeError("network"), [{"id": "free"}]])
        with self.assertRaises(RuntimeError):
            model_catalog.auto_model("A", "backend", fetch)
        self.assertEqual(model_catalog.auto_model("A", "backend", fetch), "free")
        db = MagicMock()
        with (
            patch("admin.routers.models._is_model_allowed", return_value=False),
            patch.object(proxy.model_catalog, "auto_model") as discover,
        ):
            self.assertIsNone(proxy._pick_best_model(db, "free"))
            discover.assert_not_called()

    def test_android_models_use_local_rules_without_network_and_filter_group(self):
        engine = create_engine("sqlite://")
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine)() as db:
            db.add_all(
                [
                    ModelConfig(model_id="visible", enabled=1),
                    ModelConfig(model_id="disabled", enabled=0),
                    ModelConfig(model_id="other-group", enabled=1),
                ]
            )
            db.commit()
            with (
                patch.dict(os.environ, {"WORKBUDDY_ANDROID": "1"}),
                patch.object(proxy, "get_key_row", return_value=SimpleNamespace(id=1)),
                patch.object(proxy, "check_quota"),
                patch.object(proxy, "_select_account", return_value=SimpleNamespace(id=1)),
                patch.object(proxy, "_key_group_models", return_value={"visible", "disabled"}),
                patch.object(proxy.backend, "AccountSession", side_effect=AssertionError("unexpected network")),
            ):
                result = proxy.models(db=db, x_api_key="test", authorization=None)
                self.assertEqual([m["id"] for m in result["data"]], ["visible"])
                self.assertEqual(result["source"], "local")
        engine.dispose()


class CredentialTests(unittest.TestCase):
    def test_valid_jwt_without_expires_at_does_not_refresh_for_each_header(self):
        with tempfile.TemporaryDirectory() as work:
            token = jwt.encode(
                {"exp": int(time.time()) + 3600}, "test-only-key-at-least-32-characters", algorithm="HS256"
            )
            path = Path(work) / "offline.info"
            path.write_text(
                json.dumps({"auth": {"accessToken": token, "refreshToken": "offline-refresh"}, "account": {}})
            )
            cm = converter.CredentialManager(path)
            with (
                patch.object(cm, "_refresh") as refresh,
                patch.object(converter, "client_user_agent", return_value="offline"),
                patch.object(converter, "_get_turing_device_token", return_value=None),
            ):
                cm.get_headers()
                cm.get_headers()
                refresh.assert_not_called()
            cm._cached["auth"]["expiresAt"] = int(time.time() + 3600)
            self.assertFalse(cm._is_expired())
            cm._cached["auth"].pop("expiresAt")
            cm._cached["auth"]["accessToken"] = jwt.encode(
                {"exp": int(time.time()) - 1}, "test-only-key-at-least-32-characters", algorithm="HS256"
            )
            self.assertTrue(cm._is_expired())

    def test_gateway_prepares_credentials_once_per_attempt(self):
        sess = MagicMock()
        sess.get_headers.return_value = {"Authorization": "Bearer offline"}
        with patch.object(proxy.backend, "AccountSession", return_value=sess):
            prepared = proxy._account_session_safe(MagicMock(), SimpleNamespace(auth_json="{}"))
            headers = proxy._headers_for_attempt(prepared, {"X-Session-ID": "offline-session"})
        sess.get_headers.assert_called_once_with()
        self.assertEqual(headers["X-Session-ID"], "offline-session")
        self.assertEqual(headers["Authorization"], "Bearer offline")


class MobileLatencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_disconnect_watch_does_not_count_as_upload_or_network_failure(self):
        count = 0

        async def receive():
            nonlocal count
            count += 1
            if count == 1:
                return {"type": "http.request", "body": b"{}", "more_body": False}
            await asyncio.sleep(0.06)
            return {"type": "http.disconnect"}

        async def app(scope, receive, send):
            await receive()
            trace = request_metrics.network_trace()
            trace("http11.receive_response_headers.started", {})
            await receive()
            trace("http11.receive_response_headers.failed", {"exception": asyncio.CancelledError()})

        async def send(message):
            pass

        await request_metrics.TimingMiddleware(app)(
            {"type": "http", "method": "POST", "path": "/v1/chat/completions"}, receive, send
        )
        row = request_metrics.snapshot()["recent"][0]
        self.assertGreater(row["total_ms"], 40)
        self.assertLess(next(x["ms"] for x in row["stages"] if x["name"] == "client_upload_wait"), 10)
        self.assertNotIn("network_error", row)

    async def test_upload_disconnect_is_distinct_from_invalid_json_with_same_http_response(self):
        for body, expected in ((None, "client_disconnected"), (b"{", "invalid_json"), (b"\xff", "invalid_encoding")):
            with self.subTest(expected=expected):

                async def app(scope, receive, send):
                    response = await proxy.chat_completions(Request(scope, receive), MagicMock(), "offline", None)
                    await response(scope, receive, send)

                async def receive():
                    await asyncio.sleep(0.06)
                    if body is None:
                        return {"type": "http.disconnect"}
                    return {"type": "http.request", "body": body, "more_body": False}

                messages = []

                async def send(message):
                    messages.append(message)

                request_metrics._recent.clear()
                with (
                    patch.object(proxy, "get_key_row", return_value=SimpleNamespace(id=1)),
                    patch.object(proxy, "check_quota"),
                    patch.object(proxy, "_select_account", side_effect=AssertionError("unexpected upstream")),
                ):
                    await request_metrics.TimingMiddleware(app)(
                        {"type": "http", "path": "/v1/chat/completions", "method": "POST", "headers": []}, receive, send
                    )
                row = request_metrics.snapshot()["recent"][0]
                self.assertEqual(row["status"], 400)
                self.assertEqual(row["response_bytes"], 57)
                self.assertEqual(row["request_error"], expected)
                self.assertEqual(row["outcome"], "disconnected" if body is None else "http_error")
                self.assertEqual(row["upload_complete"], body is not None)
                self.assertEqual(row["request_bytes"], len(body or b""))
                self.assertGreaterEqual(next(x["ms"] for x in row["stages"] if x["name"] == "read_request_body"), 20)
                self.assertGreaterEqual(next(x["ms"] for x in row["stages"] if x["name"] == "client_upload_wait"), 20)

    async def test_terminal_reaches_eager_closing_client_after_settlement(self):
        for terminal in (
            b"data: [DONE]\n\n",
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed"}\n\n',
        ):
            with self.subTest(terminal=terminal):
                settled = []

                async def source():
                    yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n' + terminal
                    await asyncio.sleep(0.03)
                    settled.append(True)

                stream = mobile_stream.events(source(), interval=0.01)
                async for frame in stream:
                    if frame == terminal:
                        self.assertEqual(settled, [True])
                        break
                await stream.aclose()
                self.assertEqual(settled, [True])

    async def test_heartbeats_do_not_claim_model_content_and_partial_frames_are_intact(self):
        app = FastAPI()
        app.add_middleware(request_metrics.TimingMiddleware)
        content = b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'

        async def source():
            yield content[:7]
            await asyncio.sleep(0.06)
            yield content[7:] + b"data: [DONE]\n\n"

        @app.get("/v1/chat/completions")
        async def endpoint():
            return StreamingResponse(mobile_stream.events(source(), interval=0.015), media_type="text/event-stream")

        request_metrics._recent.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.get("/v1/chat/completions")
        row = request_metrics.snapshot()["recent"][0]
        self.assertIn(b": keep-alive\n\n", response.content)
        self.assertIn(content, response.content)
        self.assertGreaterEqual(row["first_delta_ms"], 55)
        self.assertLess(row["first_body_ms"], row["first_delta_ms"])
        self.assertEqual(row["outcome"], "completed")
        self.assertTrue(row["terminal_sent"])

    async def test_level_cancellation_drains_worker_before_session_cleanup(self):
        ready, done = threading.Event(), threading.Event()
        cleanup, cancelled_result = [], []

        def worker():
            ready.set()
            time.sleep(0.1)
            done.set()
            return "worker-result"

        async def consume():
            try:
                await proxy._blocking_call(worker, on_cancel=cancelled_result.append)
            finally:
                cleanup.append(done.is_set())

        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await asyncio.to_thread(ready.wait, 2)
            group.cancel_scope.cancel()
        self.assertEqual(cleanup, [True])
        self.assertEqual(cancelled_result, ["worker-result"])

    async def test_disconnected_stream_cleans_producer_before_returning(self):
        ready, done = threading.Event(), threading.Event()
        cleanup = []

        def worker():
            ready.set()
            time.sleep(0.1)
            done.set()

        async def source():
            try:
                await proxy._blocking_call(worker)
                yield b"data: [DONE]\n\n"
            finally:
                cleanup.append(done.is_set())

        async def consume():
            async for _ in mobile_stream.events(source(), interval=0.01):
                pass

        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            await asyncio.to_thread(ready.wait, 2)
            group.cancel_scope.cancel()
        self.assertEqual(cleanup, [True])

    async def test_level_cancellation_during_admission_returns_late_granted_slot(self):
        ready = threading.Event()
        lease = gateway_limits.Admission("offline-test")

        def allow(key, rate=None, concurrent=None):
            ready.set()
            time.sleep(0.1)
            return lease

        async def app(*args):
            self.fail("cancelled request must not enter app")

        with (
            patch.object(gateway_limits, "_limits", return_value=(1, 1)),
            patch.object(gateway_limits, "_key_admission_params", return_value=("offline-test", 1, 1)),
            patch.object(gateway_limits, "allow", side_effect=allow),
            patch.object(gateway_limits, "release") as release,
        ):
            async with anyio.create_task_group() as group:
                group.start_soon(
                    gateway_limits.GatewayAdmissionMiddleware(app),
                    {"type": "http", "path": "/v1/chat/completions", "headers": [(b"x-api-key", b"offline-test")]},
                    None,
                    None,
                )
                await asyncio.to_thread(ready.wait, 2)
                group.cancel_scope.cancel()
            release.assert_called_once_with(lease)

    async def test_live_diagnostics_show_age_and_interruption_instead_of_http_success(self):
        connected = asyncio.Event()
        finish = asyncio.Event()

        async def app(scope, receive, send):
            await send(
                {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]}
            )
            await send({"type": "http.response.body", "body": b": keep-alive\n\n", "more_body": True})
            connected.set()
            await finish.wait()
            await receive()

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        middleware = request_metrics.TimingMiddleware(app)
        request_metrics._recent.clear()
        task = asyncio.create_task(middleware({"type": "http", "path": "/v1/chat/completions"}, receive, send))
        await connected.wait()
        await asyncio.sleep(0.025)
        active = request_metrics.snapshot()["active"][0]
        self.assertGreaterEqual(active["elapsed_ms"], 20)
        self.assertIsNone(active["first_delta_ms"])
        finish.set()
        await task
        row = request_metrics.snapshot()["recent"][0]
        self.assertEqual(row["status"], 200)
        self.assertEqual(row["outcome"], "disconnected")
        self.assertFalse(row["response_complete"])

    async def test_http_200_stream_without_terminal_is_interrupted_and_inband_error_is_reported(self):
        for frame, outcome in (
            (b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', "interrupted"),
            (b'data: {"error":{"message":"offline test"}}\n\n', "http_error"),
        ):
            with self.subTest(outcome=outcome):
                app = FastAPI()
                app.add_middleware(request_metrics.TimingMiddleware)

                async def source():
                    yield frame

                @app.get("/v1/chat/completions")
                async def endpoint():
                    return StreamingResponse(mobile_stream.events(source()), media_type="text/event-stream")

                request_metrics._recent.clear()
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                    response = await client.get("/v1/chat/completions")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(request_metrics.snapshot()["recent"][0]["outcome"], outcome)

    async def test_diagnostics_capture_worker_timing_without_sensitive_data(self):
        app = FastAPI()
        app.add_middleware(request_metrics.TimingMiddleware)

        @app.post("/v1/models")
        async def endpoint():
            await proxy._blocking_call(time.sleep, 0.03)
            return {"ok": True}

        request_metrics._recent.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
            response = await client.post(
                "/v1/models?secret=query-secret",
                headers={"Authorization": "Bearer key-secret"},
                json={"secret": "body-secret"},
            )
        rows = request_metrics.snapshot()["recent"]
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], response.headers["x-local-request-id"])
        self.assertGreaterEqual(rows[0]["first_body_ms"], 25)
        self.assertTrue(any(item["name"] == "sleep" and item["ms"] >= 25 for item in rows[0]["stages"]))
        self.assertNotIn("secret", json.dumps(rows))

    async def test_sqlite_write_lock_does_not_block_other_requests(self):
        with tempfile.TemporaryDirectory() as work:
            engine = create_engine(
                "sqlite:///" + (Path(work) / "probe.db").as_posix(),
                connect_args={"check_same_thread": False, "timeout": 2},
            )
            Base.metadata.create_all(engine)
            ready, release = threading.Event(), threading.Event()

            def writer():
                with engine.connect() as connection:
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                    ready.set()
                    release.wait(1)
                    connection.rollback()

            thread = threading.Thread(target=writer)
            thread.start()
            await asyncio.to_thread(ready.wait, 2)
            try:
                with patch.object(proxy, "SessionLocal", sessionmaker(bind=engine)):
                    record = asyncio.create_task(proxy._blocking_call(proxy._record_usage, 0, 0, "offline", 0, None))
                    began = time.perf_counter()
                    await asyncio.sleep(0.05)
                    self.assertLess(time.perf_counter() - began, 0.3)
                    self.assertFalse(record.done())
                    release.set()
                    self.assertIsNotNone(await record)
            finally:
                release.set()
                await asyncio.to_thread(thread.join)
                engine.dispose()

    async def test_mobile_connections_reuse_sockets_isolate_cookies_and_drop_closed_sockets(self):
        connections, cookies = [], []

        async def serve(reader, writer):
            connections.append(writer)
            try:
                while True:
                    headers = await reader.readuntil(b"\r\n\r\n")
                    cookies.append(b"cookie:" in headers.lower())
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nSet-Cookie: private=account-A\r\n\r\nOK")
                    await writer.drain()
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        app = FastAPI()
        # 1.14.0 在真实 HTTP 写前有发车闸门（upstream_dispatch.before_send），
        # 它要求 scope 里已有账号键；这条用例只验证套接字复用，直接给个固定键。
        request = SimpleNamespace(app=app, scope={"app": app, "_upstream_account_key": "test:socket-reuse"})
        url = "http://127.0.0.1:" + str(server.sockets[0].getsockname()[1])
        try:
            await upstream_http.start(app, proxy.HTTP_LIMITS)
            for attempt in range(2):
                async with upstream_http.client(request, proxy._stream_timeout(), proxy.HTTP_LIMITS) as client:
                    self.assertEqual((await client.get(url)).text, "OK")
            self.assertEqual(len(connections), 1)
            self.assertEqual(cookies, [False, False])
            self.assertIs(upstream_http.ssl_context(), upstream_http.ssl_context())
            # A peer can close its idle socket (e.g. after a network change).
            connections[0].close()
            await connections[0].wait_closed()
            await asyncio.sleep(0.02)
            async with upstream_http.client(request, proxy._stream_timeout(), proxy.HTTP_LIMITS) as client:
                self.assertEqual((await client.get(url)).text, "OK")
            self.assertEqual(len(connections), 2)
            await upstream_http.stop(app)
            await upstream_http.start(app, proxy.HTTP_LIMITS)
            async with upstream_http.client(request, proxy._stream_timeout(), proxy.HTTP_LIMITS) as client:
                await client.get(url)
            self.assertEqual(len(connections), 3)
        finally:
            await upstream_http.stop(app)
            server.close()
            await server.wait_closed()

    async def test_timing_includes_selection_and_ignores_early_empty_role(self):
        class Frames(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
                await asyncio.sleep(0.06)
                yield b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\n'
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

        def upstream(request):
            return httpx.Response(200, stream=Frames())

        real_client = httpx.AsyncClient

        @asynccontextmanager
        async def client_factory(request, timeout, limits, *, retry_budget=None):
            # 1.14.0 的 client() 会无条件塞入真实 _BorrowedTransport，
            # 因此只能在 upstream_http.client 这一层替换成 MockTransport。
            async with real_client(transport=httpx.MockTransport(upstream), timeout=timeout, limits=limits) as conn:
                yield conn

        def slow_selection(*args):
            time.sleep(0.04)
            return "free"

        body = json.dumps({"model": "free", "stream": True, "messages": [{"role": "user", "content": "hi"}]}).encode()

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request = Request(
            {
                "type": "http",
                "app": FastAPI(),
                "headers": [],
                "method": "POST",
                "path": "/v1/chat/completions",
                "client": ("127.0.0.1", 1234),
            },
            receive,
        )
        session = MagicMock()
        session.get_headers.return_value = {}
        session.updated_json.return_value = None
        pool = MagicMock()
        pool.waf.active.return_value = False
        pool.acquire.return_value = True
        log = MagicMock(return_value=1)
        replacements = {
            "get_key_row": MagicMock(return_value=SimpleNamespace(id=1)),
            "pool_budget": MagicMock(return_value=SweepBudget(1, 1)),
            "check_quota": MagicMock(),
            "_key_group_models": MagicMock(return_value=None),
            "_pick_best_model": slow_selection,
            "SessionLocal": MagicMock(),
            "_select_account": MagicMock(return_value=SimpleNamespace(id=1, uid="offline")),
            # 1.14.0 的流式路径改走带等待的选号包装（发车闸门），
            # 只 patch _select_account 不够。
            "_select_account_with_wait": AsyncMock(return_value=SimpleNamespace(id=1, uid="offline")),
            "_account_session_async": AsyncMock(return_value=session),
            "POOL": pool,
            "_note_success": MagicMock(),
            "_record_usage": MagicMock(),
            "_log_chat_row": log,
        }
        with ExitStack() as stack:
            stack.enter_context(patch.object(upstream_http, "client", client_factory))
            for name, replacement in replacements.items():
                stack.enter_context(patch.object(proxy, name, replacement))
            started = time.perf_counter()
            response = await proxy.chat_completions(request, MagicMock(), "test", None)
            first_ms = None
            from tests import collect_response

            def observe(chunk):
                nonlocal first_ms
                first_ms = first_ms or (time.perf_counter() - started) * 1000

            await collect_response(response, observe)
            measured = log.call_args.args[0]
            self.assertGreaterEqual(measured, 95)
            self.assertLess(abs(measured - first_ms), 20)


if __name__ == "__main__":
    unittest.main()
