import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    """在 tmp_path 内加载模块：模块级代码（config 加载 + runtime_state 恢复）以相对路径执行，
    import 完成后覆盖 CONFIG_FILE / RUNTIME_STATE_FILE 为绝对路径供后续调用。"""
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_cooldown_persist_{id(tmp_path)}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.CONFIG_FILE = os.path.join(tmp_path, "api_config.json")
        module.RUNTIME_STATE_FILE = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


def write_config(tmp_path, endpoints):
    with open(os.path.join(tmp_path, "api_config.json"), "w", encoding="utf-8") as f:
        json.dump({"api_endpoints": endpoints}, f)


def write_state(tmp_path, state):
    with open(os.path.join(tmp_path, "api_runtime_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f)


def read_state(tmp_path):
    with open(os.path.join(tmp_path, "api_runtime_state.json"), "r", encoding="utf-8") as f:
        return json.load(f)


EP1 = {
    "id": "ep1",
    "name": "ep1",
    "base_url": "http://example/v1",
    "api_key": "x",
    "model": "m",
    "in_pool": True,
    "pool_groups": ["main"],
}


class RuntimeCooldownPersistenceTests(unittest.TestCase):
    def test_snapshot_collects_cooldown_state(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            pool.add_endpoint(dict(EP1))
            ep = pool._endpoints[0]
            ep._cooldown_until = time.time() + 600
            ep._cooldown_reason = "quota_exceeded"
            ep._fail_count = 3
            module.pool = pool
            self.assertTrue(module.snapshot_runtime_state())
            state = read_state(tmp_path)
            self.assertIn("cooldowns", state)
            rec = state["cooldowns"]["ep1"]
            self.assertEqual(rec["cooldown_reason"], "quota_exceeded")
            self.assertEqual(rec["fail_count"], 3)
            self.assertGreater(rec["cooldown_until"], time.time())

    def test_manual_switch_save_preserves_cooldowns(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool()
            pool.add_endpoint(dict(EP1))
            ep = pool._endpoints[0]
            ep._cooldown_until = time.time() + 600
            ep._cooldown_reason = "rate_limited"
            module.pool = pool
            module.snapshot_runtime_state()
            # 模拟手动切换/模型切换的显式写盘：只更新 groups，不得抹掉 cooldowns
            self.assertTrue(module.save_runtime_state_groups({"main": "ep1"}))
            state = read_state(tmp_path)
            self.assertEqual(state["cooldowns"]["ep1"]["cooldown_reason"], "rate_limited")

    def test_startup_restores_cooldown_state(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            write_config(tmp_path, [EP1])
            write_state(tmp_path, {
                "groups": {"main": "ep1"},
                "cooldowns": {
                    "ep1": {
                        "cooldown_until": time.time() + 600,
                        "cooldown_reason": "probe_failed",
                        "fail_count": 2,
                        "manual_unlock_required": False,
                        "health": "bad",
                        "health_error": "probe timeout",
                    }
                },
            })
            module = load_module(tmp_path)
            ep = module.pool._endpoints[0]
            self.assertGreater(ep._cooldown_until, time.time())
            self.assertEqual(ep._cooldown_reason, "probe_failed")
            self.assertEqual(ep._fail_count, 2)
            self.assertFalse(ep._manual_unlock_required)
            self.assertEqual(ep._health, "bad")
            self.assertEqual(ep._health_error, "probe timeout")

    def test_startup_restores_manual_unlock(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            write_config(tmp_path, [EP1])
            write_state(tmp_path, {
                "groups": {"main": "ep1"},
                "cooldowns": {"ep1": {"manual_unlock_required": True}},
            })
            module = load_module(tmp_path)
            self.assertTrue(module.pool._endpoints[0]._manual_unlock_required)

    def test_expired_cooldown_not_restored(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            write_config(tmp_path, [EP1])
            write_state(tmp_path, {
                "groups": {"main": "ep1"},
                "cooldowns": {
                    "ep1": {"cooldown_until": time.time() - 60, "cooldown_reason": "stale"}
                },
            })
            module = load_module(tmp_path)
            ep = module.pool._endpoints[0]
            self.assertEqual(ep._cooldown_until, 0)
            self.assertEqual(ep._cooldown_reason, "")

    def test_stale_cooldown_key_cleaned(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            write_config(tmp_path, [EP1])
            write_state(tmp_path, {
                "groups": {"main": "ep1"},
                "cooldowns": {"ghost": {"cooldown_until": time.time() + 600}},
            })
            load_module(tmp_path)
            state = read_state(tmp_path)
            self.assertNotIn("ghost", state.get("cooldowns", {}))

    def test_old_format_without_cooldowns_compatible(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            write_config(tmp_path, [EP1])
            write_state(tmp_path, {"groups": {"main": "ep1"}})
            module = load_module(tmp_path)
            self.assertEqual(module.pool._endpoints[0]._cooldown_until, 0)


if __name__ == "__main__":
    unittest.main()
