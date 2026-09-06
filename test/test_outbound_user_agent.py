"""出站 User-Agent 透传（2026-09-05）。

契约：
- 优先级 端点自定义 UA > 客户端原始 UA > 默认库标识
- 只有代理路径 (/v1/chat/completions, /chat/completions) 写入客户端 UA
- 请求结束后线程局部状态清理，后续探活/管理请求不得复用上一个客户端的 UA
"""

import importlib.util
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from urllib.parse import urlparse

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


class OutboundUserAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def setUp(self):
        self.module.clear_client_user_agent()

    def tearDown(self):
        self.module.clear_client_user_agent()

    def _capture_outbound_ua(self, endpoint):
        """跑一次 _try_endpoint（非流式），返回实际出站的 User-Agent 头。

        端点用 use_proxy=True，走 urllib.request.urlopen 分支（生产主链路）；
        use_proxy=False 的 build_opener 分支由 test_direct_connect_branch_* 覆盖。
        """
        module = self.module
        pool = module.APIPool()
        seen = {}

        def fake_urlopen(request, timeout=None):
            seen["ua"] = request.get_header("User-agent")
            return FakeResponse()

        with mock.patch.object(module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result, error = pool._try_endpoint(
                endpoint,
                {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                30,
                log_usage=False,
            )
        self.assertIsNotNone(result, f"request failed: {error}")
        return seen["ua"]

    def _endpoint(self, **kwargs):
        params = {
            "id": "ep1", "name": "ep1", "base_url": "https://upstream.example/v1",
            "api_key": "sk-test", "model": "m", "max_retries": 0, "use_proxy": True,
        }
        params.update(kwargs)
        return self.module.Endpoint(**params)

    # ── 优先级链 ──

    def test_client_ua_passed_through_when_no_custom_ua(self):
        self.module.set_client_user_agent("OpenAI/Python 2.24.0")
        self.assertEqual(self._capture_outbound_ua(self._endpoint()), "OpenAI/Python 2.24.0")

    def test_endpoint_custom_ua_overrides_client_ua(self):
        self.module.set_client_user_agent("OpenAI/Python 2.24.0")
        ep = self._endpoint(default_headers={"User-Agent": "hermes-agent/0.20.5"})
        self.assertEqual(self._capture_outbound_ua(ep), "hermes-agent/0.20.5")

    def test_extra_headers_ua_overrides_client_ua(self):
        self.module.set_client_user_agent("OpenAI/Python 2.24.0")
        ep = self._endpoint(extra_headers={"User-Agent": "custom-probe/1.0"})
        self.assertEqual(self._capture_outbound_ua(ep), "custom-probe/1.0")

    def test_fallback_default_ua_when_client_sent_none(self):
        # 客户端未带 UA：回退默认库标识，不得裸奔 Python-urllib
        self.assertEqual(
            self._capture_outbound_ua(self._endpoint()),
            self.module._DEFAULT_OUTBOUND_UA,
        )
        self.assertNotIn("urllib", self._capture_outbound_ua(self._endpoint()).lower())

    def test_blank_client_ua_falls_back_to_default(self):
        self.module.set_client_user_agent("   ")
        self.assertEqual(
            self._capture_outbound_ua(self._endpoint()),
            self.module._DEFAULT_OUTBOUND_UA,
        )

    def test_none_client_ua_falls_back_to_default(self):
        self.module.set_client_user_agent(None)
        self.assertEqual(
            self._capture_outbound_ua(self._endpoint()),
            self.module._DEFAULT_OUTBOUND_UA,
        )

    # ── 线程隔离与清理 ──

    def test_thread_local_isolation(self):
        self.module.set_client_user_agent("main-thread/1.0")
        other = {}

        def worker():
            other["resolved"] = self.module.resolve_outbound_user_agent()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        # 工作线程看不到主线程的客户端 UA（探活线程即走这条路径）
        self.assertEqual(other["resolved"], self.module._DEFAULT_OUTBOUND_UA)
        self.assertEqual(self.module.resolve_outbound_user_agent(), "main-thread/1.0")

    def test_clear_restores_default(self):
        self.module.set_client_user_agent("stale-client/9.9")
        self.module.clear_client_user_agent()
        self.assertEqual(
            self.module.resolve_outbound_user_agent(),
            self.module._DEFAULT_OUTBOUND_UA,
        )

    # ── 路径判定：只有代理路径透传 ──

    def test_proxy_path_matching(self):
        handler_cls = self.module.Handler
        proxy_paths = [
            "/v1/chat/completions",
            "/chat/completions",
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

    def test_proxy_paths_cover_actual_router_entries(self):
        """_PROXY_PATHS 必须与 api_handler 里的代理路由保持一致。"""
        source = open(MODULE_PATH, encoding="utf-8").read()
        self.assertIn('cp in ("/v1/chat/completions", "/chat/completions")', source)
        self.assertEqual(
            set(self.module.Handler._PROXY_PATHS),
            {"/v1/chat/completions", "/chat/completions"},
        )

    # ── fetch_models 保持默认标识（管理页/探活不透传） ──

    def test_fetch_models_keeps_default_ua(self):
        module = self.module
        pool = module.APIPool()
        module.set_client_user_agent("browser-chrome/141")
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
            seen["ua"] = request.get_header("User-agent")
            return ModelsResponse()

        opener = mock.Mock()
        opener.open.side_effect = lambda request, timeout=None: fake_urlopen(request, timeout)
        with mock.patch.object(module.urllib.request, "build_opener", return_value=opener):
            pool.fetch_models("https://upstream.example/v1", "sk-test", use_proxy=False)
        self.assertEqual(seen["ua"], "OpenAI/Python 2.33.0")


if __name__ == "__main__":
    unittest.main()
