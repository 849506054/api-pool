"""出站客户端特征透传（2026-09-05 起，2026-09-11 扩展为整头透传）。

契约：
- 优先级 端点自定义头 > 客户端特征（真实入站头 / 客户端基线） > 默认库标识
- 只有代理路径写入真实入站头；探活/管理页测试/拉模型接口无客户端上下文 → 复用客户端基线
- 请求结束后清理线程局部状态（本线程回退默认），基线保留供无上下文路径复用
- 入站头里的连接级/池托管头（Host/Content-Length/Connection/Authorization/x-api-key/anthropic-version）不透传
"""

import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_ua_passthrough_test", MODULE_PATH)
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


CLIENT_HEADERS = {
    "User-Agent": "hermes-agent/0.21.0",
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
    "X-Stainless-Lang": "python",
    "X-Stainless-Package-Version": "2.24.0",
    "Host": "localhost:5100",
    "Content-Length": "42",
    "Connection": "keep-alive",
    "Authorization": "Bearer pool-key",
    "x-api-key": "pool-key",
    "anthropic-version": "2023-06-01",
}


class OutboundClientHeadersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def setUp(self):
        self.module.clear_client_headers()
        self.module._client_baseline.clear()

    def tearDown(self):
        self.module.clear_client_headers()
        self.module._client_baseline.clear()

    def _capture_outbound_headers(self, endpoint):
        """跑一次 _try_endpoint（非流式），返回实际出站头（全小写键）。

        端点用 use_proxy=True，走 urllib.request.urlopen 分支（生产主链路）。
        """
        module = self.module
        pool = module.APIPool()
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return FakeResponse()

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result, error = pool._try_endpoint(
                endpoint,
                {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                30,
                log_usage=False,
            )
        self.assertIsNotNone(result, f"request failed: {error}")
        return seen["headers"]

    def _endpoint(self, **kwargs):
        params = {
            "id": "ep1", "name": "ep1", "base_url": "https://upstream.example/v1",
            "api_key": "sk-test", "model": "m", "max_retries": 0, "use_proxy": True,
        }
        params.update(kwargs)
        return self.module.Endpoint(**params)

    # ── 透传 ──

    def test_client_headers_passed_through(self):
        self.module.set_client_headers(CLIENT_HEADERS)
        h = self._capture_outbound_headers(self._endpoint())
        self.assertEqual(h.get("user-agent"), "hermes-agent/0.21.0")
        self.assertEqual(h.get("accept"), "application/json")
        self.assertEqual(h.get("accept-encoding"), "gzip, deflate")
        self.assertEqual(h.get("x-stainless-package-version"), "2.24.0")

    def test_hop_by_hop_and_pool_managed_headers_dropped(self):
        self.module.set_client_headers(CLIENT_HEADERS)
        h = self._capture_outbound_headers(self._endpoint())
        # pool 按端点协议重写鉴权头，客户端带来的一律不得上线
        self.assertEqual(h.get("authorization"), "Bearer sk-test")
        self.assertNotIn("x-api-key", h)  # openai 协议不写 x-api-key；客户端带来的也必须被过滤
        self.assertNotEqual(h.get("host"), "localhost:5100")
        self.assertNotEqual(h.get("content-length"), "42")
        # Connection 由 urllib/http.client 在发送时决定（header_items 里看不到），客户端值不得放行
        self.assertNotEqual(h.get("connection"), "keep-alive")

    def test_endpoint_custom_headers_override_client(self):
        self.module.set_client_headers(CLIENT_HEADERS)
        ep = self._endpoint(default_headers={"User-Agent": "hermes-agent/0.20.5"})
        self.assertEqual(self._capture_outbound_headers(ep).get("user-agent"), "hermes-agent/0.20.5")

    def test_extra_headers_override_client(self):
        self.module.set_client_headers(CLIENT_HEADERS)
        ep = self._endpoint(extra_headers={"User-Agent": "custom-probe/1.0"})
        self.assertEqual(self._capture_outbound_headers(ep).get("user-agent"), "custom-probe/1.0")

    def test_fallback_default_ua_when_client_sent_none(self):
        h = self._capture_outbound_headers(self._endpoint())
        self.assertEqual(h.get("user-agent"), self.module._DEFAULT_OUTBOUND_UA)
        self.assertNotIn("urllib", h.get("user-agent", "").lower())

    def test_blank_and_none_client_headers_fall_back(self):
        self.module.set_client_headers({})
        self.assertEqual(self._capture_outbound_headers(self._endpoint()).get("user-agent"), self.module._DEFAULT_OUTBOUND_UA)
        self.module.set_client_headers(None)
        self.assertEqual(self._capture_outbound_headers(self._endpoint()).get("user-agent"), self.module._DEFAULT_OUTBOUND_UA)

    # ── Accept-Encoding 收敛 ──

    def test_unsupported_accept_encoding_tokens_filtered(self):
        self.module.set_client_headers({"User-Agent": "curl/8.5.0", "Accept-Encoding": "gzip, deflate, br, zstd"})
        h = self._capture_outbound_headers(self._endpoint())
        self.assertEqual(h.get("accept-encoding"), "gzip, deflate")

    def test_unsupported_only_accept_encoding_dropped(self):
        # 只有 br 时丢弃该头（让上游不压缩），而不是把 br 放行给不支持的解压链
        self.module.set_client_headers({"User-Agent": "curl/8.5.0", "Accept-Encoding": "br, zstd"})
        h = self._capture_outbound_headers(self._endpoint())
        self.assertNotIn("accept-encoding", h)

    # ── 线程隔离 / 基线 ──

    def test_probe_thread_reuses_client_baseline(self):
        self.module.set_client_headers(CLIENT_HEADERS)
        other = {}

        def worker():
            other["headers"] = self.module.resolve_outbound_headers("")

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        # 探活/测试线程无客户端上下文 → 复用基线，使上游客户端校验（如 UA 白名单）判定准确
        self.assertEqual(other["headers"].get("User-Agent"), "hermes-agent/0.21.0")
        self.assertEqual(other["headers"].get("X-Stainless-Lang"), "python")

    def test_thread_local_cleared_but_baseline_kept(self):
        self.module.set_client_headers({"User-Agent": "stale-client/9.9"})
        self.module.clear_client_headers()
        self.module._client_baseline.clear()
        self.assertEqual(self.module.resolve_outbound_headers("").get("User-Agent"), self.module._DEFAULT_OUTBOUND_UA)

    # ── 路径判定：只有代理路径捕获入站头 ──

    def test_proxy_path_matching(self):
        handler_cls = self.module.Handler
        proxy_paths = [
            "/v1/chat/completions",
            "/chat/completions",
            "/v1/responses",
            "/responses",
            "/v1/chat/completions?stream=true",
        ]
        admin_paths = [
            "/api/endpoints",
            "/api/fetch-models",
            "/api/test-model",
            "/api/test-vision",
            "/api/health-check",
            "/v1/models",
        ]

        class PathOnly:
            """只带 path 的替身：直接复用 Handler 未绑定方法，避免起真实 HTTP server。"""

            _PROXY_PATHS = handler_cls._PROXY_PATHS

            def __init__(self, path):
                self.path = path

        def is_proxy(path):
            return handler_cls._is_proxy_path(PathOnly(path))

        for path in proxy_paths:
            self.assertTrue(is_proxy(path), f"{path} should be a proxy path")
        for path in admin_paths:
            self.assertFalse(is_proxy(path), f"{path} must not be a proxy path")

    def test_proxy_handler_captures_full_header_map(self):
        """do_POST 代理分支必须把整个入站头映射交给 set_client_headers（而非只取 UA）。"""
        source = open(MODULE_PATH, encoding="utf-8").read()
        self.assertIn("set_client_headers(self.headers)", source)
        self.assertIn("clear_client_headers()", source)

    # ── fetch_models 复用同一来源 ──

    def test_fetch_models_reuses_client_baseline(self):
        module = self.module
        pool = module.APIPool()
        module.set_client_headers(CLIENT_HEADERS)
        seen = {}

        class ModelsResponse:
            def read(self):
                return b'{"data":[{"id":"m"}]}'

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return ModelsResponse()

        opener = mock.Mock()
        opener.open.side_effect = lambda request, timeout=None: fake_urlopen(request, timeout)
        module.clear_client_headers()  # 模拟管理页线程：无客户端上下文
        with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
            pool.fetch_models("https://upstream.example/v1", "sk-test", use_proxy=False)
        self.assertEqual(seen["headers"].get("user-agent"), "hermes-agent/0.21.0")
        self.assertEqual(seen["headers"].get("x-stainless-lang"), "python")
        self.assertEqual(seen["headers"].get("authorization"), "Bearer sk-test")

    def test_fetch_models_uses_profile_when_set(self):
        module = self.module
        pool = module.APIPool()
        pool._client_profiles = {"codex-tui": {"headers": {"User-Agent": "codex-tui/0.118.0", "x-app": "cli"}}}
        module.set_client_headers({"User-Agent": "hermes-agent/0.21.0"})
        seen = {}

        class ModelsResponse:
            def read(self):
                return b'{"data":[{"id":"m"}]}'

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return ModelsResponse()

        # 端点选了 profile：拉模型必须伪装成该 profile，而不是带 Hermes 头
        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            pool.fetch_models("https://upstream.example/v1", "sk-test", use_proxy=True, client_profile="codex-tui")
        self.assertEqual(seen["headers"].get("user-agent"), "codex-tui/0.118.0")
        self.assertEqual(seen["headers"].get("x-app"), "cli")
        self.assertNotIn("x-stainless-lang", seen["headers"])


if __name__ == "__main__":
    unittest.main()
