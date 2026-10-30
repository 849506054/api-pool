"""「后台探针 = 关闭检测」的端点不参与探活（2026-10-30）。

关闭检测（health_mode=none）此前仍会走 `_probe_endpoint`：函数内部直接返回「通过」、
不发 ping，但请求路径和冷却恢复路径照打「进行探活 / 探活通过」，把无检测的端点
当成探活通过来记账，日志与行为都对不上。

契约：这类端点在两处调用点被跳过，不发探测请求、不打探活日志；
冷却到期直接按可用恢复。检测已开启的端点行为不变。
"""

import importlib.util
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")

REPLY = ({"choices": [{"message": {"content": "pong"}}]}, None)


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_probe_disabled_test_{os.getpid()}_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        module.__dict__["RUNTIME_STATE_FILE"] = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class ProbeDisabledTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.module = load_module(self.tmp.name)
        self.pool = self.module.pool
        self.addCleanup(self._drop_endpoints)

    def _drop_endpoints(self):
        with self.pool._lock:
            self.pool._endpoints.clear()

    def _add(self, ep_id, priority, health_mode, in_pool=True):
        ep = self.module.Endpoint(
            id=ep_id, name=ep_id, base_url="http://127.0.0.1:1", api_key="test",
            model="m-" + ep_id, priority=priority, in_pool=in_pool, enabled=True,
            use_proxy=False, protocol="openai", pool_groups=["main"],
            health_mode=health_mode,
        )
        with self.pool._lock:
            self.pool._endpoints.append(ep)
        return ep

    def _patch_probe(self, calls):
        def fake_probe(ep, *a, **kw):
            calls.append(ep.id)
            return True, ""

        patcher = mock.patch.object(self.pool, "_probe_endpoint", side_effect=fake_probe)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_request_path_skips_probe_for_disabled_candidate(self):
        """候选端点关闭检测：不发探活，直接发起真实请求。"""
        self._add("ep1", 1, "chat")
        self._add("ep2", 2, "none")
        probes, attempts = [], []
        self._patch_probe(probes)

        def fake_try(ep, *a, **kw):
            attempts.append(ep.id)
            if ep.id == "ep1":
                return None, "HTTP 500: upstream error"
            return REPLY

        with mock.patch.object(self.pool, "_try_endpoint", side_effect=fake_try):
            result = self.pool.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(result["choices"][0]["message"]["content"], "pong")
        self.assertIn("ep2", attempts, "关闭检测的候选端点应直接被使用")
        self.assertNotIn("ep2", probes, "关闭检测的候选端点不该被探活")

    def test_chat_mode_candidate_still_probed(self):
        """对照：检测开启（chat）的候选端点照常探活。"""
        self._add("ep1", 1, "chat")
        self._add("ep2", 2, "chat")
        probes, attempts = [], []
        self._patch_probe(probes)

        def fake_try(ep, *a, **kw):
            attempts.append(ep.id)
            if ep.id == "ep1":
                return None, "HTTP 500: upstream error"
            return REPLY

        with mock.patch.object(self.pool, "_try_endpoint", side_effect=fake_try):
            self.pool.chat([{"role": "user", "content": "hi"}])

        self.assertIn("ep2", probes, "检测开启的候选端点应照常探活")

    def test_cooldown_recovery_skips_probe_for_disabled_endpoint(self):
        """关闭检测的端点冷却到期：不探活，直接清冷却恢复。"""
        ep = self._add("ep1", 1, "none")
        with self.pool._lock:
            ep._cooldown_until = time.time() - 1
            ep._cooldown_reason = "HTTP 429"
            ep._health = "bad"
        probes = []
        self._patch_probe(probes)

        self.pool._background_probe(ep, {})

        self.assertEqual(probes, [], "关闭检测的端点不该被探活")
        self.assertEqual(ep._cooldown_until, 0, "冷却应被清除")
        self.assertEqual(ep._fail_count, 0, "失败计数应清零")
        self.assertEqual(ep._health, "ok")


if __name__ == "__main__":
    unittest.main()
