"""客户端伪装 profile 管理 API（2026-09-10）。

契约：
- GET /api/client-profiles → 内建（builtin=True）+ 自定义列表
- POST 新建/覆盖自定义；保留头过滤；重名内建拒绝（400）
- PUT/DELETE 按名操作；内建不可删（400）
- 保存后 _sync_to_config 落盘 client_profiles 键
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        spec = importlib.util.spec_from_file_location("api_pool_profiles_api_test", MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class ClientProfilesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as tmp_path:
            cls.module = load_module(tmp_path)

    def setUp(self):
        # API 内的 _sync_to_config 写 CONFIG_FILE（相对路径），chdir 到隔离目录
        self._orig_cwd = os.getcwd()
        self._tmp = tempfile.mkdtemp()
        os.chdir(self._tmp)
        self.module.pool._client_profiles = {}

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_list_includes_builtin_hermes(self):
        code, data, _ = self.module.api_handler("GET", "/api/client-profiles", {})
        self.assertEqual(code, 200)
        by_name = {p["name"]: p for p in data["profiles"]}
        self.assertIn("hermes", by_name)
        self.assertTrue(by_name["hermes"]["builtin"])
        self.assertIn("X-Stainless-Lang", by_name["hermes"]["headers"])

    def test_create_custom_profile(self):
        code, data, _ = self.module.api_handler("POST", "/api/client-profiles", {
            "name": "my-cli",
            "headers": {"User-Agent": "my-cli/1.0", "X-App": "cli"},
        })
        self.assertEqual(code, 201, data)
        code, data, _ = self.module.api_handler("GET", "/api/client-profiles", {})
        names = {p["name"]: p for p in data["profiles"]}
        self.assertIn("my-cli", names)
        self.assertFalse(names["my-cli"]["builtin"])
        self.assertEqual(names["my-cli"]["headers"]["User-Agent"], "my-cli/1.0")

    def test_reserved_headers_filtered_on_save(self):
        code, data, _ = self.module.api_handler("POST", "/api/client-profiles", {
            "name": "bad",
            "headers": {"Authorization": "Bearer evil", "Host": "x", "Accept": "application/json"},
        })
        self.assertEqual(code, 201)
        code, data, _ = self.module.api_handler("GET", "/api/client-profiles", {})
        h = {p["name"]: p for p in data["profiles"]}["bad"]["headers"]
        self.assertNotIn("Authorization", h)
        self.assertNotIn("Host", h)
        self.assertEqual(h.get("Accept"), "application/json")

    def test_builtin_cannot_be_overwritten_or_deleted(self):
        code, data, _ = self.module.api_handler("POST", "/api/client-profiles", {
            "name": "hermes", "headers": {"User-Agent": "hacked"},
        })
        self.assertEqual(code, 400)
        code, data, _ = self.module.api_handler("DELETE", "/api/client-profiles/hermes", {})
        self.assertEqual(code, 400)

    def test_update_and_delete_custom(self):
        self.module.api_handler("POST", "/api/client-profiles", {
            "name": "cli", "headers": {"User-Agent": "v1"},
        })
        code, data, _ = self.module.api_handler("PUT", "/api/client-profiles/cli", {
            "headers": {"User-Agent": "v2", "X-Extra": "1"},
        })
        self.assertEqual(code, 200)
        code, data, _ = self.module.api_handler("GET", "/api/client-profiles", {})
        h = {p["name"]: p for p in data["profiles"]}["cli"]["headers"]
        self.assertEqual(h["User-Agent"], "v2")
        self.assertEqual(h["X-Extra"], "1")
        code, data, _ = self.module.api_handler("DELETE", "/api/client-profiles/cli", {})
        self.assertEqual(code, 200)
        code, data, _ = self.module.api_handler("DELETE", "/api/client-profiles/nope", {})
        self.assertEqual(code, 400)

    def test_save_persists_to_config(self):
        self.module.api_handler("POST", "/api/client-profiles", {
            "name": "persist-me",
            "headers": {"User-Agent": "p/1.0"},
        })
        with open("api_config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("client_profiles", cfg)
        self.assertEqual(cfg["client_profiles"]["persist-me"]["headers"]["User-Agent"], "p/1.0")

    def test_custom_profile_resolves_outbound(self):
        # 端点到自定义 profile 的完整链路：保存 → 端点引用 → 出站头
        self.module.api_handler("POST", "/api/client-profiles", {
            "name": "cli", "headers": {"User-Agent": "cli/9.9", "X-App": "cli"},
        })
        ep = self.module.Endpoint(
            id="e1", name="e1", base_url="https://up.example/v1", api_key="sk",
            model="m", max_retries=0, use_proxy=True, client_profile="cli",
        )
        seen = {}

        class R:
            status = 200
            headers = {}

            def read(self, size=-1):
                return b'{"choices":[{"message":{"role":"assistant","content":"ok"}}],"usage":{}}'

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return R()

        with mock.patch.object(self.module.urllib.request, "urlopen", side_effect=fake_urlopen):
            result, error = self.module.pool._try_endpoint(
                ep, {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": False},
                30, log_usage=False,
            )
        self.assertIsNotNone(result, error)
        self.assertEqual(seen["headers"].get("user-agent"), "cli/9.9")
        self.assertEqual(seen["headers"].get("x-app"), "cli")


if __name__ == "__main__":
    unittest.main()
