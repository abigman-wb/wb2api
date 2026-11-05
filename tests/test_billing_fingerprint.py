"""计费指纹整行删除 + 刷新令牌字段继承。

背景（MiraiHub 2026-11-05 实测，补52）：Claude Code 把渠道指纹当一行纯文本塞进
system（`x-anthropic-billing-header: cc_version=...; cc_entrypoint=sdk-cli;`），
上游只认官方客户端。本仓原实现只在 `sanitize=True` 的链路上删它，而
`/v1/chat/completions` 走 `normalize_harness_body()`，从不打开那个开关 ——
带指纹的辅助请求因此裸发，被上游 400/11128 拒绝，客户端反复重试。
"""

import json
import unittest
from unittest.mock import patch

import upstream_compat
from converter import CredentialManager

HDR = "x-anthropic-billing-header: cc_version=2.1.310.8ea; cc_entrypoint=sdk-cli;"


class BillingFingerprintTests(unittest.TestCase):
    def test_system_line_removed_text_kept(self):
        body = {"messages": [
            {"role": "system", "content": f"{HDR}\nYou are a helpful assistant."},
            {"role": "user", "content": "hi"},
        ]}
        stats = upstream_compat.normalize_harness_body(body)
        self.assertEqual(stats["billing"], 1)
        system = body["messages"][0]["content"]
        self.assertNotIn("x-anthropic-billing-header", system)
        self.assertIn("helpful assistant", system)
        self.assertNotIn(";", system.split("\n")[0])

    def test_inline_keeps_user_text(self):
        body = {"messages": [
            {"role": "system", "content": f"{HDR} 请只回答 JSON。"},
        ]}
        upstream_compat.strip_harness_billing_header(body)
        content = body["messages"][0]["content"]
        self.assertNotIn("x-anthropic-billing-header", content)
        self.assertIn("请只回答 JSON", content)

    def test_empty_text_block_removed(self):
        body = {"messages": [
            {"role": "system", "content": [
                {"type": "text", "text": HDR},
                {"type": "text", "text": "keep me"},
            ]},
        ]}
        upstream_compat.normalize_harness_body(body)
        parts = body["messages"][0]["content"]
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["text"], "keep me")

    def test_message_only_header_dropped(self):
        body = {"messages": [
            {"role": "system", "content": HDR},
            {"role": "user", "content": "hi"},
        ]}
        upstream_compat.normalize_harness_body(body)
        self.assertEqual(len(body["messages"]), 1)
        self.assertEqual(body["messages"][0]["role"], "user")

    def test_user_content_untouched(self):
        body = {"messages": [
            {"role": "user", "content": f"日志里出现了 {HDR}"},
        ]}
        before = json.loads(json.dumps(body))
        upstream_compat.normalize_harness_body(body)
        self.assertEqual(body, before)

    def test_prepare_upstream_body_also_strips(self):
        body = {"messages": [{"role": "system", "content": f"{HDR}\nbody"}]}
        upstream_compat.prepare_upstream_body(body)
        self.assertNotIn("x-anthropic-billing-header", body["messages"][0]["content"])


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload, **_kwargs):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *_args, **_kwargs):
        return _FakeResponse(self._payload)


class RefreshInheritanceTests(unittest.TestCase):
    def _manager(self):
        raw = json.dumps({"account": {"uid": "1"}, "auth": {
            "accessToken": "old.access", "refreshToken": "old.refresh",
            "sessionState": "st-1", "tokenType": "Bearer", "domain": "cn",
            "scope": "openid", "enterpriseId": "ent-1",
            "expiresIn": 2592000, "refreshExpiresIn": 5183999,
        }})
        return CredentialManager.from_json(raw)

    def test_partial_payload_inherits_fields(self):
        mgr = self._manager()
        payload = {"code": 0, "data": {"accessToken": "new.access"}}
        with patch("converter.httpx.Client", lambda *a, **k: _FakeClient(payload)):
            mgr._refresh()
        auth = json.loads(mgr.export_json())["auth"]
        self.assertEqual(auth["accessToken"], "new.access")
        for name in ("refreshToken", "sessionState", "tokenType", "domain",
                     "scope", "enterpriseId", "expiresIn", "refreshExpiresIn"):
            self.assertTrue(auth.get(name), f"{name} 未被继承")

    def test_refresh_token_rotation_kept(self):
        mgr = self._manager()
        payload = {"code": 0, "data": {
            "accessToken": "new.access", "refreshToken": "new.refresh"}}
        with patch("converter.httpx.Client", lambda *a, **k: _FakeClient(payload)):
            mgr._refresh()
        auth = json.loads(mgr.export_json())["auth"]
        self.assertEqual(auth["refreshToken"], "new.refresh")


if __name__ == "__main__":
    unittest.main()
