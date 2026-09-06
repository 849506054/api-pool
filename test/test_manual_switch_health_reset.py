"""手动切换语义：用户断言优先于观测态（2026-09-06）。

手动切换携带用户的主观判断（刚续费、上游已恢复），优先于 API Pool 上一次观测
推导出的不健康状态：先视为恢复健康并真的发请求，再由真实结果重新分类。
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
        spec = importlib.util.spec_from_file_location(
            f"api_pool_manual_switch_{id(tmp_path)}", MODULE_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class ManualSwitchClearsHealthTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, groups=None, **kwargs):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model="mdl", priority=priority, in_pool=True,
            use_proxy=False, cooldown_minutes=5,
            pool_groups=list(groups) if groups else ["main"], **kwargs,
        )

    @staticmethod
    def collect_hits(pool):
        hits = []

        def fake_try(ep, payload, timeout, **kwargs):
            hits.append(ep.id)
            return {"choices": [{"message": {"content": "ok"}}]}, ""

        pool._try_endpoint = fake_try
        return hits

    def test_cooled_endpoint_switch_actually_serves_next_request(self):
        """冷却中的端点被手动切换后，下一次请求必须真的落到它上面。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            cooling = self.endpoint(module, "cooling", 1)
            healthy = self.endpoint(module, "healthy", 2)
            pool = module.APIPool([cooling, healthy])
            cooling._fail_count = 3
            cooling._last_error = "HTTP 503: simulated"
            cooling._cooldown_reason = "server_error"
            cooling._health = "bad"
            cooling._health_error = "probe timeout"
            pool._set_cooldown(cooling)
            pool._set_current(pool.MAIN_GROUP, healthy.id)
            self.assertGreater(cooling._cooldown_until, time.time())

            self.assertTrue(pool.switch_to_endpoint(cooling.id))

            # 观测态已按用户断言重置为待验证
            self.assertEqual(cooling._cooldown_until, 0)
            self.assertEqual(cooling._cooldown_reason, "")
            self.assertEqual(cooling._fail_count, 0)
            self.assertEqual(cooling._last_error, "")
            self.assertEqual(cooling._health, "unknown")
            self.assertEqual(cooling._health_error, "")

            # 候选集里可见，且请求真的命中它
            candidates, _ = pool._group_sticky_candidates(pool.MAIN_GROUP)
            self.assertIn(cooling.id, [ep.id for ep in candidates])
            hits = self.collect_hits(pool)
            pool.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(hits, ["cooling"])

    def test_balance_locked_endpoint_switch_needs_no_separate_unfreeze(self):
        """余额不足（仅手动解冻）端点可直接手动切换，无需先点解冻。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            locked = self.endpoint(module, "locked", 1)
            healthy = self.endpoint(module, "healthy", 2)
            pool = module.APIPool([locked, healthy])
            pool._set_capacity_cooldown(locked, "insufficient balance")
            self.assertTrue(locked._manual_unlock_required)
            pool._set_current(pool.MAIN_GROUP, healthy.id)

            self.assertTrue(pool.switch_to_endpoint(locked.id))
            self.assertFalse(locked._manual_unlock_required)

            hits = self.collect_hits(pool)
            pool.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(hits, ["locked"])

    def test_quota_frozen_endpoint_switch_serves_request(self):
        """配额不足冻结（默认 5h）的端点手动切换后立即可用。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            frozen = self.endpoint(module, "frozen", 1)
            healthy = self.endpoint(module, "healthy", 2)
            pool = module.APIPool([frozen, healthy])
            kind, _ = pool._set_capacity_cooldown(frozen, "quota exceeded")
            self.assertEqual(kind, "quota_exceeded")
            self.assertGreater(frozen._cooldown_until, time.time())
            pool._set_current(pool.MAIN_GROUP, healthy.id)

            self.assertTrue(pool.switch_to_endpoint(frozen.id))
            hits = self.collect_hits(pool)
            pool.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(hits, ["frozen"])

    def test_manual_pointer_survives_success_on_switched_endpoint(self):
        """切换后的端点请求成功，手动指针不被 _on_success 抹除。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            cooling = self.endpoint(module, "cooling", 1)
            healthy = self.endpoint(module, "healthy", 2)
            pool = module.APIPool([cooling, healthy])
            cooling._fail_count = 2
            pool._set_cooldown(cooling)
            pool._set_current(pool.MAIN_GROUP, healthy.id)

            self.assertTrue(pool.switch_to_endpoint(cooling.id))
            hits = self.collect_hits(pool)
            pool.chat([{"role": "user", "content": "hi"}])
            pool.chat([{"role": "user", "content": "again"}])

            self.assertEqual(hits, ["cooling", "cooling"])
            self.assertEqual(pool._get_manual(pool.MAIN_GROUP), "cooling")
            self.assertEqual(pool._get_current(pool.MAIN_GROUP), "cooling")
            self.assertEqual(cooling._health, "ok")

    def test_switch_is_escape_hatch_when_whole_pool_is_cooled(self):
        """全池冷却时手动切换必须能作为强制逃生阀，不再抛「没有可用的 API 端点」。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            first = self.endpoint(module, "first", 1)
            second = self.endpoint(module, "second", 2)
            pool = module.APIPool([first, second])
            for ep in (first, second):
                ep._fail_count = 2
                pool._set_cooldown(ep)

            self.assertTrue(pool.switch_to_endpoint(first.id))
            hits = self.collect_hits(pool)
            pool.chat([{"role": "user", "content": "hi"}])
            self.assertEqual(hits, ["first"])

    def test_switch_still_refuses_disabled_or_out_of_pool(self):
        """enabled/in_pool 是配置声明，不属于可推翻的观测态。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            disabled = self.endpoint(module, "disabled", 1, enabled=False)
            out_of_pool = self.endpoint(module, "out", 2)
            out_of_pool.in_pool = False
            pool = module.APIPool([disabled, out_of_pool])

            self.assertFalse(pool.switch_to_endpoint("disabled"))
            self.assertFalse(pool.switch_to_endpoint("out"))
            self.assertIsNone(pool._get_manual(pool.MAIN_GROUP))

    def test_switch_does_not_reset_configured_usage_budgets(self):
        """daily_limit/rpm_limit 是用户配置的预算，切换不重置既发用量。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            capped = self.endpoint(module, "capped", 1, daily_limit=1000, rpm_limit=5)
            pool = module.APIPool([capped])
            capped._today_date = module.datetime.now().strftime("%Y-%m-%d")
            capped._today_used = 1200
            for _ in range(5):
                capped._req_timestamps.append(time.time())

            self.assertTrue(pool.switch_to_endpoint(capped.id))
            self.assertEqual(capped._today_used, 1200)
            self.assertEqual(len(capped._req_timestamps), 5)
            self.assertTrue(pool._is_quota_exceeded(capped))
            self.assertTrue(pool._is_rpm_limited(capped))

    def test_switch_rest_persists_cleared_cooldown_snapshot(self):
        """REST 切换必须同步落盘冷却快照，避免崩溃重启复活已解除的冻结。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.add_endpoint({
                "id": "ep1", "name": "ep1", "base_url": "http://127.0.0.1:1",
                "api_key": "test", "model": "mdl", "priority": 1,
                "in_pool": True, "pool_groups": ["main"],
            })
            ep = module.pool._endpoints[0]
            ep._cooldown_until = time.time() + 900
            ep._cooldown_reason = "quota_exceeded"
            ep._fail_count = 3
            ep._health = "bad"
            module.snapshot_runtime_state()
            with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                self.assertIn("ep1", json.load(handle)["cooldowns"])

            status, response, _ = module.api_handler(
                "POST", "/api/switch-endpoint/ep1", None,
            )
            self.assertEqual((status, response["ok"]), (200, True))

            with open(module.RUNTIME_STATE_FILE, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["groups"]["main"], "ep1")
            self.assertNotIn("ep1", state.get("cooldowns", {}))


if __name__ == "__main__":
    unittest.main()
