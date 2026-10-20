"""轮转预算耗尽后的收尾契约（2026-10-20）。

预算见底时请求不再出站：不探活、不发请求、不把「未发出」记成端点失败，
也就不会冷却任何端点——除了在预算耗尽前已经真实失败的那个端点。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parents[1] / "api_pool_server.py"


def load_module(cwd):
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        name = f"budget_contract_{time.time_ns()}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous)


def endpoint(ep_id, priority):
    return {
        "id": ep_id, "name": ep_id, "base_url": "http://127.0.0.1:1/v1",
        "api_key": "test-key", "model": "test-model", "priority": priority,
        "enabled": True, "in_pool": True, "pool_groups": ["main"],
        "timeout": 5, "cooldown_minutes": 5, "health_mode": "models",
    }


def write_config(path):
    payload = {
        "api_endpoints": [endpoint("ep1", 1), endpoint("ep2", 2), endpoint("ep3", 3)],
        "pool_group_defs": [{"name": "main", "type": "mixed", "model": "api-pool"}],
    }
    Path(path).write_text(json.dumps(payload))


class BudgetExhaustedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "api_config.json")
        write_config(self.config)
        self.module = load_module(self.tmp.name)
        self.pool = self.module.pool
        self.cooled = []

    def _install(self, try_endpoint, probes):
        def fake_probe(ep, *a, **kw):
            probes.append(ep.id)
            raise AssertionError(f"预算耗尽后不该探活: {ep.id}")

        self.patcher_try = mock.patch.object(self.pool, "_try_endpoint", side_effect=try_endpoint)
        self.patcher_probe = mock.patch.object(self.pool, "_probe_endpoint", side_effect=fake_probe)
        self.patcher_try.start()
        self.patcher_probe.start()
        self.addCleanup(self.patcher_try.stop)
        self.addCleanup(self.patcher_probe.stop)

    def _assert_untouched(self):
        for ep in self.pool._endpoints:
            self.assertEqual(ep._fail_count, 0, f"{ep.id} 被记了失败")
            self.assertEqual(ep._cooldown_until, 0, f"{ep.id} 被冷却")

    def test_budget_gone_before_first_attempt_sends_nothing(self):
        self.pool._FALLBACK_DEADLINE_SECONDS = 0
        probes, attempts = [], []

        def never_called(*a, **kw):
            attempts.append(1)
            raise AssertionError("预算耗尽后不该发请求")

        self._install(never_called, probes)
        with self.assertRaises(self.module.AllEndpointsFailed) as ctx:
            self.pool.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(attempts, [])
        self.assertEqual(probes, [])
        self._assert_untouched()
        self.assertTrue(any("预算" in e for e in ctx.exception.errors))

    def test_budget_expiring_after_real_failure_stops_before_probe(self):
        self.pool._FALLBACK_DEADLINE_SECONDS = 0.3
        probes, attempts = [], []

        def slow_timeout(*a, **kw):
            attempts.append(1)
            time.sleep(0.4)
            return None, "连接/超时错误: The read operation timed out"

        self._install(slow_timeout, probes)
        with self.assertRaises(self.module.AllEndpointsFailed):
            self.pool.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(len(attempts), 1, "预算耗尽后仍继续尝试了其他端点")
        self.assertEqual(probes, [], "预算耗尽后仍探活了候选端点")
        failed = next(ep for ep in self.pool._endpoints if ep.id == "ep1")
        self.assertGreater(failed._cooldown_until, 0, "真实失败的端点应照常冷却")
        for ep_id in ("ep2", "ep3"):
            ep = next(ep for ep in self.pool._endpoints if ep.id == ep_id)
            self.assertEqual(ep._cooldown_until, 0, f"{ep_id} 没收到请求却被冷却")
            self.assertEqual(ep._fail_count, 0, f"{ep_id} 没收到请求却被记失败")

    def test_not_sent_failure_never_cools_down(self):
        probes = []
        self._install(lambda *a, **kw: (None, self.module._BUDGET_EXHAUSTED_MSG), probes)
        with self.assertRaises(self.module.AllEndpointsFailed):
            self.pool.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(probes, [], "预算耗尽后仍探活了候选端点")
        self._assert_untouched()


if __name__ == "__main__":
    unittest.main()
