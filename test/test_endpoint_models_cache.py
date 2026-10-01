"""端点模型目录缓存（2026-10-01）：重复调用不打上游、连接指纹变更自动失效、TTL 过期重取。

背景：UI 展开聚合池/模型下拉与后台预取会频繁调 `/api/endpoints/<id>/models`，
每次都是真上游往返（实测 0.2~4 秒）。缓存后同一连接指纹在 TTL 内只打一次上游。
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
        name = f"api_pool_models_cache_test_{os.getpid()}_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp_path, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class EndpointModelsCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_module(self.tmp.name)
        self.pool = self.module.APIPool()
        self.module.pool = self.pool
        self.ep = self.module.Endpoint(
            id="ep-1", name="ep-1", site_name="site-a",
            base_url="https://site-a.example/v1", api_key="secret-a", model="m1",
            priority=1, priority_by_group={}, timeout=17, enabled=True, in_pool=True,
            use_proxy=False, protocol="openai", pool_groups=["main"],
        )
        self.pool.add_endpoint(self.ep)

    def test_repeat_call_fetches_upstream_once(self):
        with mock.patch.object(self.pool, "fetch_models", return_value=[{"id": "m1"}]) as fetch:
            first = self.pool.fetch_endpoint_models("ep-1")
            second = self.pool.fetch_endpoint_models("ep-1")
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(first, [{"id": "m1"}])
        self.assertEqual(second, first)

    def test_connection_change_invalidates_cache(self):
        with mock.patch.object(self.pool, "fetch_models", return_value=[{"id": "m1"}]) as fetch:
            self.pool.fetch_endpoint_models("ep-1")
            self.ep.api_key = "secret-b"          # 连接指纹变化 → 必须重新拉取
            self.pool.fetch_endpoint_models("ep-1")
            self.ep.base_url = "https://other.example/v1"
            self.pool.fetch_endpoint_models("ep-1")
        self.assertEqual(fetch.call_count, 3)

    def test_ttl_expiry_refetches(self):
        with mock.patch.object(self.pool, "fetch_models", return_value=[{"id": "m1"}]) as fetch:
            self.pool.fetch_endpoint_models("ep-1")
            for key, (ts, models) in list(self.pool._models_cache.items()):
                self.pool._models_cache[key] = (ts - self.pool._MODELS_CACHE_TTL - 1, models)
            self.pool.fetch_endpoint_models("ep-1")
        self.assertEqual(fetch.call_count, 2)

    def test_empty_result_not_cached(self):
        with mock.patch.object(self.pool, "fetch_models", return_value=[]) as fetch:
            self.pool.fetch_endpoint_models("ep-1")
            self.pool.fetch_endpoint_models("ep-1")
        self.assertEqual(fetch.call_count, 2)

    def test_unknown_endpoint_raises_keyerror(self):
        with self.assertRaises(KeyError):
            self.pool.fetch_endpoint_models("nope")
        self.assertEqual(self.pool._models_cache, {})


if __name__ == "__main__":
    unittest.main()
