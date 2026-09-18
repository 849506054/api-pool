"""子组使用 main 空闲工作端点（2026-09-18 需求 1/2）。

契约：端点同时属于 main 与子组、且是 main 的当前/手动工作端点时，main 在该端点
空闲满窗口（main 组实体 idle_seconds）后，子组不再避让它 —— 它回到子组的正常候选
序列按组内优先级参与选择，而不是「候选耗尽才兜底」。

覆盖：
- 空闲满窗口 → 该端点进入子组正常候选
- 未满窗口 / 窗口=0（默认） / 端点有在途 → 仍被剔除（行为同现状）
- 手动指定的 main 工作端点同样适用
- main 侧候选不受影响（单向互斥未变）
- 组字段校验与配置落盘往返（/api/groups 字段 idle_seconds）
"""

import importlib.util
import os
import sys
import tempfile
import threading
import time
import unittest

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_idle_use_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        module.__dict__["RUNTIME_STATE_FILE"] = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class IdleBorrowTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, groups):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model="mdl", priority=priority,
            in_pool=True, use_proxy=False, pool_groups=groups,
        )

    def make_pool(self, module):
        """shared 同时属 main 与 bg，且是 main 当前工作端点；b1/b2 为 bg 自有成员。"""
        shared = self.endpoint(module, "shared", 1, ["main", "bg"])
        b1 = self.endpoint(module, "b1", 2, ["bg"])
        b2 = self.endpoint(module, "b2", 3, ["bg"])
        pool = module.APIPool([shared, b1, b2])
        pool._set_current("main", "shared")
        return pool, shared

    def test_idle_main_endpoint_joins_subgroup_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            shared._last_success_ts = time.time() - 700

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertIn("shared", [ep.id for ep in candidates])

    def test_before_window_elapses_still_avoided(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            shared._last_success_ts = time.time() - 60

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertNotIn("shared", [ep.id for ep in candidates])

    def test_window_disabled_matches_current_behavior(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            shared._last_success_ts = time.time() - 700  # 默认 idle_seconds=0=关闭

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertNotIn("shared", [ep.id for ep in candidates])

    def test_inflight_main_endpoint_still_avoided(self):
        """main 正在用（在途）时不算空闲，子组仍避让。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            shared._last_success_ts = time.time() - 700
            pool._acquire_inflight("shared", "main")

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertNotIn("shared", [ep.id for ep in candidates])

    def test_manually_selected_main_endpoint_also_eligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_current("main", None)
            pool._set_manual("main", "shared")
            pool._set_group_idle_seconds("main", 600)
            shared._last_success_ts = time.time() - 700

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertIn("shared", [ep.id for ep in candidates])

    def test_group_priority_orders_borrowed_endpoint(self):
        """借来的端点按子组组内优先级参与排序，不享有特权位次。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            shared._last_success_ts = time.time() - 700
            pool._set_ep_priority(shared, "bg", 9)  # 组内优先级最低

            candidates, _ = pool._group_sticky_candidates("bg")
            candidates.sort(key=lambda e: pool._ep_priority(e, "bg"))
            self.assertEqual(candidates[-1].id, "shared")

    def test_main_side_unaffected(self):
        """main 组候选不受空闲窗口影响（单向互斥未变）。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, _ = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)

            candidates, _ = pool._group_sticky_candidates("main")
            self.assertEqual(sorted(ep.id for ep in candidates), ["shared"])

    def test_validation_and_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, _ = self.make_pool(module)

            self.assertIsNone(pool._valid_group_idle_seconds(5))        # 低于下限
            self.assertIsNone(pool._valid_group_idle_seconds(86401))    # 高于上限
            self.assertIsNone(pool._valid_group_idle_seconds("abc"))
            self.assertEqual(pool._valid_group_idle_seconds(0), 0)      # 0=关闭
            self.assertEqual(pool._valid_group_idle_seconds("600"), 600)

            ok, msg = pool.create_group("bg2", "mixed", "", 0, 5)
            self.assertFalse(ok)
            self.assertIn("空闲时间非法", msg)

            ok, _ = pool.update_group("main", {"idle_seconds": 300})
            self.assertTrue(ok)
            self.assertEqual(pool._group_idle_seconds("main"), 300)
            reloaded = pool._load_group_defs([{"name": "main", "idle_seconds": 300}])
            self.assertEqual(reloaded["main"]["idle_seconds"], 300)
            ok, _ = pool.update_group("main", {"idle_seconds": 0})
            self.assertTrue(ok)
            self.assertEqual(pool._group_idle_seconds("main"), 0)
            self.assertNotIn("idle_seconds", pool._group_defs["main"])


if __name__ == "__main__":
    unittest.main()
