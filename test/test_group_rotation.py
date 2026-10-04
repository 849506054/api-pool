"""组级端点轮换 rotate_minutes（2026-10-05）：按组内优先级定时推进当前端点。

覆盖：归一化校验 / 默认态不落键 / 轮换循环推进 / 手动指针跳过 / 冷却成员跳过 /
tick 到期调度 / 持久化往返。
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_rotation_test_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        module.__dict__["RUNTIME_STATE_FILE"] = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class GroupRotationTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, groups=None):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id,
            base_url="http://127.0.0.1:1", api_key="test", model="m",
            priority=priority, enabled=True, in_pool=True, use_proxy=False,
            pool_groups=groups or ["bg"],
        )

    def pool_with_bg(self, module, n=3):
        eps = [self.endpoint(module, f"b{i}", i, ["bg"]) for i in range(1, n + 1)]
        pool = module.APIPool(eps)
        pool._derive_group_defs()
        return pool, eps

    def test_normalization_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            self.assertEqual(pool._valid_group_rotate_minutes(0), 0)
            self.assertEqual(pool._valid_group_rotate_minutes(None), 0)
            self.assertEqual(pool._valid_group_rotate_minutes("30"), 30)
            self.assertEqual(pool._valid_group_rotate_minutes(1), 1)
            self.assertEqual(pool._valid_group_rotate_minutes(1440), 1440)
            self.assertIsNone(pool._valid_group_rotate_minutes(-1))
            self.assertIsNone(pool._valid_group_rotate_minutes(1441))
            self.assertIsNone(pool._valid_group_rotate_minutes("abc"))

    def test_default_zero_does_not_store_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            ok, msg = pool.create_group("bg", "mixed", "", 0, 0, "all", 0)
            self.assertTrue(ok, msg)
            self.assertNotIn("rotate_minutes", pool._group_defs["bg"])

    def test_create_and_update_store_rotate_minutes(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            ok, _ = pool.create_group("bg", "mixed", "", 0, 0, "all", 5)
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["rotate_minutes"], 5)
            self.assertEqual(pool._group_rotate_minutes("bg"), 5)
            # 非法拒绝
            ok, msg = pool.create_group("bad", "mixed", "", 0, 0, "all", 9999)
            self.assertFalse(ok)
            self.assertIn("轮换", msg)
            # 编辑更新与清零（0 → 不落键）
            ok, _ = pool.update_group("bg", {"rotate_minutes": 10})
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["rotate_minutes"], 10)
            ok, _ = pool.update_group("bg", {"rotate_minutes": 0})
            self.assertTrue(ok)
            self.assertNotIn("rotate_minutes", pool._group_defs["bg"])

    def test_rotate_advances_by_priority_wraps(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_current("bg", "b1")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b2")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b3")
            pool._rotate_group_once("bg")  # 循环回第 1 名
            self.assertEqual(pool._get_current("bg"), "b1")

    def test_rotate_from_unset_goes_to_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._rotate_group_once("bg")  # 无指针 → 推进到第 1 名
            self.assertEqual(pool._get_current("bg"), "b1")

    def test_rotate_respects_manual_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_current("bg", "b1")
            pool._set_manual("bg", "b3")
            pool._rotate_group_once("bg")  # 手动固定 = 用户意图，跳过
            self.assertEqual(pool._get_current("bg"), "b1")
            pool._set_manual("bg", None)
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b2")

    def test_rotate_skips_cooldown_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, eps = self.pool_with_bg(m)
            pool._set_current("bg", "b1")
            eps[1]._cooldown_until = m.time.time() + 600  # b2 冷却中
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b3")  # 候选里 b2 被剔除

    def test_rotate_single_member_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m, n=1)
            pool._set_current("bg", "b1")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b1")

    def test_tick_only_fires_when_interval_elapsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_group_rotate_minutes("bg", 30)
            pool._set_current("bg", "b1")
            pool._rotate_groups_tick()  # 刚设间隔 → 未到期，不动
            self.assertEqual(pool._get_current("bg"), "b1")
            pool._rotate_last_ts["bg"] -= 31 * 60
            pool._rotate_groups_tick()  # 到期 → 推进一次
            self.assertEqual(pool._get_current("bg"), "b2")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "", 0, 0, "all", 15)
            m._sync_to_config()
            saved = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
            entry = next(d for d in saved["pool_group_defs"] if d["name"] == "bg")
            self.assertEqual(entry["rotate_minutes"], 15)
            raw = m.load_group_defs_config()
            pool2 = m.APIPool([])
            pool2._load_group_defs(raw)
            self.assertEqual(pool2._group_rotate_minutes("bg"), 15)
            # all 默认态：0 不落键、读回 0
            self.assertEqual(pool2._group_rotate_minutes("main"), 0)


if __name__ == "__main__":
    unittest.main()
