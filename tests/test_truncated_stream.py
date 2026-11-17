"""截断流回归（审计 H1）：上游 200 后给了正文增量、却没有终止帧时，
三条协议 × 流式/非流式都必须**不记 success**：

- chat 流式：补终止帧（finish_reason=stop + [DONE]），error_kind=upstream_internal；
- chat 聚合：换号重试，绝不把半截内容当成功 JSON 返回；
- responses 流式/非流式：绝不合成 response.completed；
- anthropic 流式/非流式：绝不合成 message_stop / 正常 message；
- 有第二个健康账号时，客户端无感拿到完整结果。
"""

import importlib
import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx

from tests.test_key_account_binding import Fixture

proxy = importlib.import_module("admin.routers.proxy")
from admin import upstream_http  # noqa: E402

# 上游给了正文增量，但没有 finish_reason=stop、没有 [DONE] —— 典型截断
TRUNCATED = (
    "data: "
    + json.dumps({"id": "t", "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]})
    + "\n\n"
)
SUCCESS = (
    "data: "
    + json.dumps({"id": "t", "choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": None}]})
    + "\n\n"
    + "data: "
    + json.dumps({"id": "t", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                  "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})
    + "\n\n"
    + "data: [DONE]\n\n"
)


class Credential:
    def __init__(self, a):
        self.a = a

    def get_headers(self, extra=None):
        return {"test-account": str(self.a.id)}

    def updated_json(self):
        return self.a.auth_json

    def close(self):
        pass


class TruncatedStreamTest(Fixture, unittest.IsolatedAsyncioTestCase):
    async def _run(self, protocol, streaming, transport, token="private-key"):
        """按 test_key_account_binding 的同款夹具驱动一次请求，返回 (output, usage_records)。"""
        handler = {"chat": proxy.chat_completions,
                   "responses": proxy.responses_proxy,
                   "messages": proxy.anthropic_messages}[protocol]
        payload = {"model": "free", "stream": streaming,
                   "messages": [{"role": "user", "content": "hi"}], "max_tokens": 20}
        if protocol == "responses":
            payload = {"model": "free", "stream": streaming, "input": "hi"}

        async def receive():
            return {"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}

        request = __import__("fastapi").Request(
            {"type": "http", "method": "POST", "path": "/", "headers": [],
             "client": ("127.0.0.1", 1)}, receive)

        usage_records = []

        def record_usage(*args, **kwargs):
            usage_records.append(kwargs.get("error_kind") or "")

        async def backoff(n):
            pass

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
                from tests import collect_response
                output = (await collect_response(result)).decode()
        return output, usage_records

    async def test_chat_stream_truncated_gets_terminal_stub_and_error(self):
        async def transport(req):
            return httpx.Response(200, text=TRUNCATED, headers={"content-type": "text/event-stream"})
        output, usage = await self._run("chat", True, transport)
        self.assertIn("finish_reason", output)
        self.assertIn("[DONE]", output)
        self.assertTrue(usage and usage[-1] == "upstream_internal", usage)
        self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_chat_aggregate_truncated_is_not_success(self):
        async def transport(req):
            return httpx.Response(200, text=TRUNCATED, headers={"content-type": "text/event-stream"})
        output, usage = await self._run("chat", False, transport)
        # 单账号、重试预算耗尽 → 必须以错误收场，而不是半截内容的成功 JSON
        self.assertNotIn('"finish_reason":"stop"', output.replace(" ", ""))
        self.assertTrue(usage and usage[-1] == "upstream_internal", usage)
        self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_responses_stream_truncated_never_synthesizes_completed(self):
        async def transport(req):
            return httpx.Response(200, text=TRUNCATED, headers={"content-type": "text/event-stream"})
        output, usage = await self._run("responses", True, transport)
        self.assertNotIn("response.completed", output)
        self.assertIn("error", output)
        self.assertTrue(usage and usage[-1] == "upstream_internal", usage)
        self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_responses_nonstream_truncated_is_error(self):
        async def transport(req):
            return httpx.Response(200, text=TRUNCATED, headers={"content-type": "text/event-stream"})
        output, usage = await self._run("responses", False, transport)
        self.assertNotIn('"status":"completed"', output.replace(" ", ""))
        self.assertTrue(usage and usage[-1] == "upstream_internal", usage)
        self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_anthropic_stream_truncated_never_synthesizes_stop(self):
        async def transport(req):
            return httpx.Response(200, text=TRUNCATED, headers={"content-type": "text/event-stream"})
        output, usage = await self._run("messages", True, transport)
        self.assertNotIn("message_stop", output)
        self.assertIn("error", output)
        self.assertTrue(usage and usage[-1] == "upstream_internal", usage)
        self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_anthropic_nonstream_truncated_is_error(self):
        async def transport(req):
            return httpx.Response(200, text=TRUNCATED, headers={"content-type": "text/event-stream"})
        output, usage = await self._run("messages", False, transport)
        self.assertNotIn('"type":"message"', output.replace(" ", "")[:200])
        self.assertTrue(usage and usage[-1] == "upstream_internal", usage)
        self.assertFalse(proxy.POOL.inflight.snapshot())

    async def test_retry_rotates_to_healthy_account(self):
        """坏号截断、好号健康 → 客户端无感拿到完整结果（流式与聚合都换号）。"""
        bad_id = self.a.id

        async def transport(req):
            acc = int(req.headers["test-account"])
            body = TRUNCATED if acc == bad_id else SUCCESS
            return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

        for protocol, streaming in (("chat", True), ("chat", False), ("responses", True), ("messages", True)):
            with self.subTest(protocol=protocol, stream=streaming):
                with self.factory() as reset:
                    for a in reset.query(__import__("admin.models", fromlist=["Account"]).Account):
                        a.cool_until = a.breaker_until = a.degrade_until = None
                        a.cool_kind = ""
                    reset.commit()
                output, _usage = await self._run(protocol, streaming, transport, token="public-key")
                self.assertIn("hello", output, output[:300])
                self.assertNotIn("partial", output)
                self.assertFalse(proxy.POOL.inflight.snapshot())


if __name__ == "__main__":
    unittest.main()
