"""客户端伪装 Client Profile（2026-09-10）。

契约：
- client_profile="hermes" 时出站头集合 == 黄金样本（Hermes openai SDK 2.24.0 实测）
- 合并优先级：客户端UA < profile < default_headers < extra_headers
- 无 profile 时行为与基线逐字节一致
- 未知 profile 名降级不伪装 + WARN，不 500
- profile 保留头（Host/Content-Length/Authorization/x-api-key/anthropic-version）被过滤
- 探活 / fetch_models 不套用 profile
"""

import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_client_profile_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class FakeResponse:
    status = 200
    headers = {}

    def read(self):
        return b'{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{}}'

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


GOLDEN = {
    "user-agent": "hermes-agent/0.21.0",
    "accept": "application/json",
    "accept-encoding": "gzip, deflate",
    "x-stainless-lang": "python",
    "x-stainless-package-version": "2.24.0",
    "x-stainless-os": "Linux",
    "x-stainless-arch": "x64",
    "x-stainless-runtime": "CPython",
    "x-stainless-runtime-version": "3.13.5",
    "x-stainless-async": "false",
    "x-stainless-retry-count": "0",
    "x-stainless-read-timeout": "600",
}


class ClientProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def setUp(self):
        self.module.clear_client_user_agent()

    def _capture_outbound_headers(self, endpoint, payload=None, profiles=None):
        module = self.module
        pool = module.APIPool()
        if profiles is not None:
            pool._client_profiles = profiles
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse()

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result, error = pool._try_endpoint(
                endpoint,
                payload or {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                30,
                log_usage=False,
            )
        self.assertIsNotNone(result, f"request failed: {error}")
        return seen["headers"]

    def _capture_with_ua(self, endpoint, ua):
        self.module.set_client_user_agent(ua)
        try:
            return self._capture_outbound_headers(endpoint)
        finally:
            self.module.set_client_user_agent("")

    def _endpoint(self, **kwargs):
        params = {
            "id": "ep1", "name": "ep1", "base_url": "https://upstream.example/v1",
            "api_key": "sk-test", "model": "m", "max_retries": 0, "use_proxy": True,
        }
        params.update(kwargs)
        return self.module.Endpoint(**params)

    # ── profile 生效 ──

    def test_profile_applies_full_golden_headers(self):
        h = self._capture_outbound_headers(self._endpoint(client_profile="hermes"))
        for k, v in GOLDEN.items():
            self.assertEqual(h.get(k), v, f"missing/mismatch {k}")

    def test_no_profile_keeps_legacy_headers_only(self):
        h = self._capture_outbound_headers(self._endpoint())
        self.assertEqual(h.get("user-agent"), self.module._DEFAULT_OUTBOUND_UA)
        self.assertNotIn("x-stainless-lang", h)

    # ── 优先级链 ──

    def test_endpoint_default_headers_override_profile(self):
        ep = self._endpoint(client_profile="hermes", default_headers={"User-Agent": "custom/1.0"})
        h = self._capture_outbound_headers(ep)
        self.assertEqual(h.get("user-agent"), "custom/1.0")
        self.assertEqual(h.get("x-stainless-lang"), "python")  # 其余头仍来自 profile

    def test_extra_headers_override_profile(self):
        ep = self._endpoint(client_profile="hermes", extra_headers={"X-Stainless-Lang": "js"})
        h = self._capture_outbound_headers(ep)
        self.assertEqual(h.get("x-stainless-lang"), "js")

    # ── 降级与保护 ──

    def test_unknown_profile_falls_back_cleanly(self):
        h = self._capture_outbound_headers(self._endpoint(client_profile="no-such"))
        self.assertEqual(h.get("user-agent"), self.module._DEFAULT_OUTBOUND_UA)
        self.assertNotIn("x-stainless-lang", h)

    def test_sanitize_filters_reserved_headers(self):
        module = self.module
        clean, dropped = module._sanitize_profile_headers({
            "User-Agent": "x", "Host": "evil.example", "Authorization": "Bearer x",
            "x-api-key": "k", "anthropic-version": "2023-06-01",
            "Content-Length": "99", "Accept": "application/json",
        })
        self.assertEqual(
            sorted(dropped),
            ["Authorization", "Content-Length", "Host", "anthropic-version", "x-api-key"],
        )
        self.assertEqual(clean, {"User-Agent": "x", "Accept": "application/json"})

    def test_builtin_profile_present_and_marked(self):
        module = self.module
        self.assertIn("hermes", module._BUILTIN_CLIENT_PROFILES)
        self.assertTrue(module._BUILTIN_CLIENT_PROFILES["hermes"].get("builtin"))

    # ── 探活/管理不套 profile ──

    def test_fetch_models_does_not_use_profile(self):
        module = self.module
        pool = module.APIPool()
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}

            class R:
                status = 200
                headers = {}

                def read(self):
                    return b'{"data":[{"id":"m"}]}'

                def close(self):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *e):
                    return False

            return R()

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            pool.fetch_models("https://upstream.example/v1", "sk-test", timeout=10, use_proxy=True)
        self.assertNotIn("x-stainless-lang", seen["headers"])

    def test_profile_reserved_header_never_reaches_wire(self):
        module = self.module
        ep = self._endpoint(client_profile="bad")
        h = self._capture_outbound_headers(
            ep,
            profiles={"bad": {"headers": {"Authorization": "Bearer evil", "X-App": "cli"}}},
        )
        # Authorization 是 pool 按端点协议加的鉴权头；profile 里的保留头不得覆盖它
        self.assertEqual(h.get("authorization"), "Bearer sk-test")
        self.assertNotIn("Bearer evil", h.get("authorization", ""))
        self.assertEqual(h.get("x-app"), "cli")


    def test_auto_matches_hermes_ua(self):
        h = self._capture_with_ua(self._endpoint(client_profile="auto"), "hermes-agent/0.21.0")
        for k, v in GOLDEN.items():
            self.assertEqual(h.get(k), v, f"missing/mismatch {k}")

    def test_auto_unknown_ua_passthrough(self):
        h = self._capture_with_ua(self._endpoint(client_profile="auto"), "curl/8.5.0")
        self.assertEqual(h.get("user-agent"), "curl/8.5.0")  # 客户端 UA 透传（既有机制，未套 profile）
        self.assertNotIn("x-stainless-lang", h)

    def test_auto_empty_ua_passthrough(self):
        h = self._capture_with_ua(self._endpoint(client_profile="auto"), "")
        self.assertNotIn("x-stainless-lang", h)

    def test_auto_respects_default_headers_override(self):
        ep = self._endpoint(client_profile="auto", default_headers={"User-Agent": "custom/2.0"})
        h = self._capture_with_ua(ep, "hermes-agent/0.21.0")
        self.assertEqual(h.get("user-agent"), "custom/2.0")
        self.assertEqual(h.get("x-stainless-lang"), "python")


if __name__ == "__main__":
    unittest.main()
