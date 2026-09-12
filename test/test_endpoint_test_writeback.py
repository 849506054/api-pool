"""定向测试端点（端点卡片 🧪）结果真实写回主池端点。

背景（2026-09-06）：原实现为端点卡片测试创建临时 APIPool 副本再请求，成功/失败
处置全部落在临时对象上，日志却使用真实端点名，造成"已触发冷却"的假象而主池
端点状态不变。契约：定向测试结果必须写回主池端点，复用正式故障分类（余额/配额/
限流/普通冷却/客户端类错误不冻结），且不轮转、不改路由指针。
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_test_endpoint_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        # 2026-09-12：池自身出站需确定身份；测试默认模拟"已有真实客户端打过池"。
        module._client_baseline.update({"User-Agent": "pytest-client/1.0"})
        return module
    finally:
        os.chdir(previous_cwd)


class TestEndpointWritebackTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority=1, **kwargs):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model="mdl", priority=priority, in_pool=True,
            use_proxy=False, cooldown_minutes=5, pool_groups=["main"], **kwargs,
        )

    def make_pool(self, module, ep):
        pool = module.APIPool([ep])
        self.assertTrue(pool.get_endpoint(ep.id) is ep)
        return pool

    def test_failed_test_records_observation_only_for_non_capacity(self):
        """测试失败（非容量类）命中主池真实端点、只写观测态：不冷却、不计失败次数（2026-09-12 伤害隔离）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "target")
            pool = self.make_pool(module, ep)

            def fake_try(target, payload, timeout, **kwargs):
                self.assertIs(target, ep)
                self.assertEqual(payload["model"], "mdl")
                return None, "HTTP 503: simulated upstream failure"

            pool._try_endpoint = fake_try
            result, error = pool.test_endpoint(ep, message="hi")
            self.assertIsNone(result)
            self.assertIn("503", error)
            self.assertEqual(ep._health, "bad")
            self.assertEqual(ep._health_error, "HTTP 503: simulated upstream failure")
            self.assertEqual(ep._last_error, "HTTP 503: simulated upstream failure")
            # 伤害隔离：非容量类失败不写路由态
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._cooldown_reason, "")
            self.assertEqual(ep._fail_count, 0)

    def test_failed_test_does_not_rotate_or_touch_other_endpoints(self):
        """测试失败不轮转当前端点，也不影响其他端点状态。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            target = self.endpoint(module, "target", 1)
            other = self.endpoint(module, "other", 2)
            pool = module.APIPool([target, other])
            pool._set_current(pool.MAIN_GROUP, other.id)
            pool._try_endpoint = lambda ep, payload, timeout, **kw: (None, "HTTP 502: gw")
            pool.test_endpoint(target, message="hi")
            self.assertEqual(target._cooldown_until, 0, "非容量类测试失败不再冷却（伤害隔离）")
            self.assertIn("502", target._last_error)
            self.assertEqual(pool._get_current(pool.MAIN_GROUP), other.id)
            self.assertEqual(other._cooldown_until, 0)
            self.assertEqual(other._fail_count, 0)

    def test_successful_test_resets_failure_state(self):
        """测试成功把端点恢复为 ok 并清除冷却/失败计数。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "target")
            pool = self.make_pool(module, ep)
            ep._fail_count = 2
            ep._last_error = "HTTP 500: old"
            ep._health = "bad"
            pool._set_cooldown(ep)
            pool._try_endpoint = lambda ep_, payload, timeout, **kw: (
                {"choices": [{"message": {"content": "pong"}}]}, ""
            )
            result, error = pool.test_endpoint(ep, message="hi")
            self.assertEqual(error, "")
            self.assertEqual(ep._health, "ok")
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._fail_count, 0)
            self.assertEqual(ep._last_error, "")

    def test_client_error_test_does_not_freeze(self):
        """客户端类错误（畸形 payload）测试失败不冻结端点。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "target")
            pool = self.make_pool(module, ep)
            pool._try_endpoint = lambda ep_, payload, timeout, **kw: (
                None, "HTTP 400: Invalid schema for function"
            )
            pool.test_endpoint(ep, message="hi")
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._fail_count, 0)
            self.assertEqual(ep._last_error, "HTTP 400: Invalid schema for function")

    def test_balance_error_locks_manual_unlock(self):
        """测试命中余额不足 → manual_unlock_required，仅手动解冻。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "target")
            pool = self.make_pool(module, ep)
            pool._try_endpoint = lambda ep_, payload, timeout, **kw: (
                None, "HTTP 403: insufficient balance"
            )
            pool.test_endpoint(ep, message="hi")
            self.assertTrue(ep._manual_unlock_required)
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._health, "bad")

    def test_rest_test_endpoint_uses_real_pool_and_404_for_missing(self):
        """/api/test 必须走主池端点对象；未知 id 返回 404。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "real-ep")
            module.pool = module.APIPool([ep])
            hits = []

            def fake_try(target, payload, timeout, **kwargs):
                hits.append(target.id)
                return {"choices": [{"message": {"content": "ok"}}]}, ""

            module.pool._try_endpoint = fake_try
            status, body, _ = module.api_handler(
                "POST", "/api/test", {"id": "real-ep", "message": "hi"}
            )
            self.assertEqual((status, body["ok"]), (200, True))
            self.assertEqual(hits, ["real-ep"])

            status, body, _ = module.api_handler(
                "POST", "/api/test", {"id": "missing", "message": "hi"}
            )
            self.assertEqual(status, 404)

    def test_rest_failed_test_persists_cooldown_snapshot_for_capacity(self):
        """/api/test 命中容量类失败（配额）仍冷却并落盘；重启不复活已冻结前的旧状态（2026-09-12：非容量类只写观测态、不落盘冷却）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "real-ep")
            module.pool = module.APIPool([ep])
            module.pool._try_endpoint = lambda ep_, payload, timeout, **kw: (
                None, "HTTP 429: rate limit exceeded, retry-after: 300"
            )
            status, body, _ = module.api_handler(
                "POST", "/api/test", {"id": "real-ep", "message": "hi"}
            )
            self.assertEqual((status, body["ok"]), (200, False))
            self.assertGreater(ep._cooldown_until, time.time())
            with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertIn("real-ep", state.get("cooldowns", {}))


if __name__ == "__main__":
    unittest.main()
