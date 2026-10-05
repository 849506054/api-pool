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


    def test_avoided_endpoint_defers_then_returns_after_takeover_finishes(self):
        """借用回切（2026-10-05 定稿）：只看「接手端点执行完没」——组内池活动停满
        300s 即释放并回切，**不要求 main 空闲**；回迁后 main 若再抢占，后续请求重新避让。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            pool._set_current("bg", "b1")
            b1 = next(e for e in pool._endpoints if e.id == "b1")
            b1.deferrable = False  # 隔离缓存保护因子，只考察 main 占用
            shared._last_success_ts = time.time() - 60  # main 正在用

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertNotIn("shared", [ep.id for ep in candidates])
            self.assertIn("bg", shared._defer_until_by_group)  # 已挂延迟回迁

            # 新建组实体前的候选计算：bg1 未配 deferrable=False 时，这里 pool_active=True
            # 但 b1.deferrable=False → 缓存保护判定为「不保护」→ 立即回迁（既有语义）。
            # 隔离缓存保护因子用 b1.deferrable=True + pool_active：
            b1.deferrable = True
            pool._last_pool_activity = time.time()
            pool._reconcile_deferred()
            self.assertEqual(pool._get_current("bg"), "b1")  # 保护缓存 → 不回迁
            self.assertIn("bg", shared._defer_until_by_group)  # 继续等接手端点跑完

            # 池活动停满窗口（接手端点执行完）→ 释放并回切，
            # **即使 main 仍在使用该端点**（回迁后 main 若再抢占，后续请求重新避让）
            pool._last_pool_activity = time.time() - 400
            pool._reconcile_deferred()
            self.assertEqual(pool._get_current("bg"), "shared")
            self.assertNotIn("bg", shared._defer_until_by_group)

            # 回迁后 main 仍占用该端点 → 下一条 bg 请求重新避让、defer 重新挂上，
            # 组内由其他端点接手（状态机：回迁 → 再避让 → 接手，循环自洽）
            cands2, _ = pool._group_sticky_candidates("bg")
            self.assertNotIn("shared", [ep.id for ep in cands2])
            self.assertIn("bg", shared._defer_until_by_group)
            pool._reconcile_deferred()  # 池活动仍停着 → 再次回迁
            self.assertEqual(pool._get_current("bg"), "shared")

    def test_return_waits_for_cache_protection_of_current_endpoint(self):
        """当前端点保护缓存且池活跃：main 空闲也先不回切（与既有延迟回迁同一口径）。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            pool._set_current("bg", "b1")  # b1.deferrable 默认 True
            pool._last_pool_activity = time.time()
            shared._last_success_ts = time.time() - 60
            pool._group_sticky_candidates("bg")  # 触发挂 defer
            self.assertIn("bg", shared._defer_until_by_group)

            shared._last_success_ts = time.time() - 700  # main 已空闲
            pool._reconcile_deferred()
            self.assertEqual(pool._get_current("bg"), "b1")  # 缓存保护未解除 → 不回切

            pool._last_pool_activity = time.time() - 4000  # 池空闲
            pool._reconcile_deferred()
            self.assertEqual(pool._get_current("bg"), "shared")  # 释放并回切

    def test_disabled_window_creates_no_defer(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_current("bg", "b1")
            shared._last_success_ts = time.time() - 700  # 窗口=0=关闭

            candidates, _ = pool._group_sticky_candidates("bg")
            self.assertNotIn("shared", [ep.id for ep in candidates])
            self.assertEqual(shared._defer_until_by_group, {})  # 不挂 defer（行为同现状）
            pool._reconcile_deferred()
            self.assertEqual(pool._get_current("bg"), "b1")

    def test_manual_pin_does_not_block_defer_and_is_cleared_on_return(self):
        """A 口径（同轮换）：手动钉住不阻止挂 defer；回切时清除该组锁定并落指针到被借端点。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_group_idle_seconds("main", 600)
            pool._set_current("bg", "b1")
            b1 = next(e for e in pool._endpoints if e.id == "b1")
            b1.deferrable = False
            pool._set_manual("bg", "b2")  # 开机恢复/用户手动：本组被钉在 b2
            shared._last_success_ts = time.time() - 60

            pool._group_sticky_candidates("bg")
            self.assertIn("bg", shared._defer_until_by_group)  # 锁定不阻止挂

            shared._last_success_ts = time.time() - 700  # main 空闲窗口满足
            pool._reconcile_deferred()
            self.assertEqual(pool._get_current("bg"), "shared")  # 回切
            self.assertIsNone(pool._get_manual("bg"))  # 锁定被回切清除（同轮换 A 口径）

    def test_rotate_enabled_group_does_not_mount_defer(self):
        """配了轮换的组不挂 defer：轮换本就是组内循环意图，回切会与之拉锯。"""
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._group_defs["bg"] = {"type": "mixed", "model": "bg"}  # 组实体（否则组级字段无处落）
            pool._set_group_idle_seconds("main", 600)
            pool._set_group_rotate_requests("bg", 5)
            self.assertEqual(pool._group_rotate_requests("bg"), 5)  # 前置条件自检
            pool._set_current("bg", "b1")
            shared._last_success_ts = time.time() - 60

            pool._group_sticky_candidates("bg")
            self.assertEqual(shared._defer_until_by_group, {})


if __name__ == "__main__":
    unittest.main()
