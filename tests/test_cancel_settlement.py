"""客户端取消记账回归（审计 H3）：客户端断开/请求被取消时，上游可能已扣积分，
此前正常记账只发生在成功收尾 → 完全漏账。修复后三条协议在取消路径都要
补一条 error_kind=client_disconnected 的保守结算记录。

复现方式：上游先吐一段正文增量，然后挂起不结束；消费端拿到第一个 chunk 后
取消任务 → 断言取消路径补记了 usage（且积分 > 0）。
"""

import asyncio
import contextlib
import importlib
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from tests.test_key_account_binding import Fixture

proxy = importlib.import_module("admin.routers.proxy")
from admin import upstream_http  # noqa: E402

CHUNK = (
    "data: "
    + json.dumps({"id": "t", "choices": [{"index": 0, "delta": {"content": "hello " * 50}, "finish_reason": None}]})
    + "\n\n"
)


async def _hang_after_chunk():
    yield CHUNK.encode()
    await asyncio.sleep(3600)


class Credential:
    def __init__(self, a):
        self.a = a

    def get_headers(self, extra=None):
        return {"test-account": str(self.a.id)}

    def updated_json(self):
        return self.a.auth_json

    def close(self):
        pass


class CancelSettlementTest(Fixture, unittest.IsolatedAsyncioTestCase):
    async def _run_cancelled(self, protocol, token="private-key"):
        handler = {"chat": proxy.chat_completions,
                   "responses": proxy.responses_proxy,
                   "messages": proxy.anthropic_messages}[protocol]
        payload = {"model": "free", "stream": True,
                   "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}
        if protocol == "responses":
            payload = {"model": "free", "stream": True, "input": "hi"}

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        request = __import__("fastapi").Request(
            {"type": "http", "method": "POST", "path": "/", "headers": [],
             "client": ("127.0.0.1", 1)}, receive)

        usage_records = []

        def record_usage(*args, **kwargs):
            usage_records.append({"credits": args[3] if len(args) > 3 else None,
                                  "error_kind": kwargs.get("error_kind") or ""})

        async def backoff(n):
            pass

        def transport(req):
            return httpx.Response(200, content=_hang_after_chunk(),
                                  headers={"content-type": "text/event-stream"})

        original = httpx.AsyncClient

        @asynccontextmanager
        async def fake_client(request, timeout, limits, *, retry_budget=None):
            async with original(transport=httpx.MockTransport(transport), timeout=timeout, limits=limits) as conn:
                yield conn

        async def session(db, a):
            return Credential(a)

        with self.factory() as reset:
            for a in reset.query(__import__("admin.models", fromlist=["Account"]).Account):
                a.cool_until = a.breaker_until = a.degrade_until = None
                a.cool_kind = ""
            reset.commit()
        with (
            patch.object(proxy, "_account_session_async", session),
            patch.object(upstream_http, "client", fake_client),
            patch.object(proxy.pool, "rotate_backoff_async", backoff),
            patch.object(proxy, "_record_usage", record_usage),
        ):
            with self.factory() as db:
                result = await handler(request, db=db, x_api_key=token, authorization=None)
                # 按 MobileResponse 的 ASGI 契约驱动：收到第一个正文 chunk 后
                # 发 http.disconnect —— 与真实客户端中途断开同一条生产路径。
                got_body = asyncio.Event()
                disconnect_event = asyncio.Event()

                async def asgi_receive():
                    await disconnect_event.wait()
                    return {"type": "http.disconnect"}

                async def asgi_send(msg):
                    if msg["type"] == "http.response.body":
                        body = msg.get("body") or b""
                        if b'"content"' in body or b'data:' in body:
                            got_body.set()

                async def drive():
                    await result({"type": "http", "method": "POST", "path": "/", "headers": [],
                                  "client": ("127.0.0.1", 1), "state": {}},
                                 asgi_receive, asgi_send)

                driver = asyncio.create_task(drive())
                await asyncio.wait_for(got_body.wait(), timeout=10)
                disconnect_event.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait_for(driver, timeout=15)
        return usage_records

    async def test_chat_cancel_settles(self):
        usage = await self._run_cancelled("chat")
        kinds = [u["error_kind"] for u in usage]
        self.assertIn("client_disconnected", kinds, usage)
        rec = [u for u in usage if u["error_kind"] == "client_disconnected"][0]
        self.assertIsNotNone(rec["credits"])
        self.assertGreater(rec["credits"], 0)

    async def test_responses_cancel_settles(self):
        usage = await self._run_cancelled("responses")
        kinds = [u["error_kind"] for u in usage]
        self.assertIn("client_disconnected", kinds, usage)
        rec = [u for u in usage if u["error_kind"] == "client_disconnected"][0]
        self.assertGreater(rec["credits"], 0)

    async def test_anthropic_cancel_settles(self):
        usage = await self._run_cancelled("messages")
        kinds = [u["error_kind"] for u in usage]
        self.assertIn("client_disconnected", kinds, usage)
        rec = [u for u in usage if u["error_kind"] == "client_disconnected"][0]
        self.assertGreater(rec["credits"], 0)

    async def test_estimate_helper_uses_raw_fallback(self):
        est = proxy._cancelled_usage_estimate("data: x\n\n" * 40)
        self.assertGreater(est["total_tokens"], 0)
        self.assertGreater(est["credits"], 0)


if __name__ == "__main__":
    unittest.main()
