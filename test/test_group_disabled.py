"""池卡 ⏸ = 组级停用/启用（2026-10-06）。

契约：池卡片的 ⏸ 只控制该端点是否参与**当前池组**业务——成员资格（pool_groups）与组内
优先级保留、全局 enabled 不动；其他组照常轮转。全局启用/禁用只由左侧端点列表的 ⏸ 管。

覆盖：
- 组级停用只影响该组候选，其他组不受影响，全局 enabled 不变
- _ep_routable_in_group 按组判定（指针回落口径）
- 停用本组当前/手动指针端点 → 清该组指针
- 手动切换（⚡）到本组已停用端点 → 自动解除本组停用
- 成员关系变更（移出组 / 出池）→ 清理 disabled_groups 残留
- 已加入 main 的子组成员在本组停用 → 不被 main 借用
- REST 路由：带 group = 组级；不带 group = 全局；不在该组 → 404
- 配置加载归一（非列表 → []）与落盘往返
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
        name = f"api_pool_group_disabled_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        module.__dict__["CONFIG_FILE"] = os.path.join(tmp_path, "api_config.json")
        module.__dict__["RUNTIME_STATE_FILE"] = os.path.join(tmp_path, "api_runtime_state.json")
        return module
    finally:
        os.chdir(previous_cwd)


class GroupDisabledTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, groups):
        return module.Endpoint(
            id=endpoint_id, name=endpoint_id, base_url="http://127.0.0.1:1",
            api_key="test", model="mdl", priority=priority,
            in_pool=True, use_proxy=False, pool_groups=groups,
        )

    def make_pool(self, module):
        """shared 同属 main+bg；b1 只属 bg。"""
        shared = self.endpoint(module, "shared", 1, ["main", "bg"])
        b1 = self.endpoint(module, "b1", 2, ["bg"])
        pool = module.APIPool([shared, b1])
        module.pool = pool
        return pool, shared

    def test_disable_in_group_only_affects_that_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)

            self.assertTrue(pool.toggle_group_disabled("shared", "bg"))

            self.assertNotIn("shared", [ep.id for ep in pool._group_sticky_candidates("bg")[0]])
            self.assertIn("shared", [ep.id for ep in pool._group_sticky_candidates("main")[0]])
            self.assertTrue(shared.enabled)  # 全局开关未被触碰
            self.assertEqual(shared.disabled_groups, ["bg"])

            # 再切一次 = 恢复参与本组
            self.assertFalse(pool.toggle_group_disabled("shared", "bg"))
            self.assertIn("shared", [ep.id for ep in pool._group_sticky_candidates("bg")[0]])

    def test_routable_in_group_and_failover_active_respect_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool.toggle_group_disabled("shared", "bg")

            self.assertFalse(pool._ep_routable_in_group(shared, "bg"))
            self.assertTrue(pool._ep_routable_in_group(shared, "main"))
            self.assertNotIn("shared", [ep.id for ep in pool._failover_active("bg")])
            self.assertIn("shared", [ep.id for ep in pool._failover_active("main")])

    def test_disable_clears_group_pointer_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._set_current("bg", "shared")
            pool._set_manual("bg", "shared")
            pool._set_current("main", "shared")

            pool.toggle_group_disabled("shared", "bg")

            self.assertIsNone(pool._get_current("bg"))
            self.assertIsNone(pool._get_manual("bg"))
            self.assertEqual(pool._get_current("main"), "shared")  # 其他组指针不动

    def test_manual_switch_clears_group_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool.toggle_group_disabled("shared", "bg")

            self.assertTrue(pool.switch_to_endpoint("shared", "bg"))

            self.assertEqual(shared.disabled_groups, [])
            self.assertEqual(pool._get_manual("bg"), "shared")

    def test_membership_change_prunes_disabled_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool.toggle_group_disabled("shared", "bg")

            pool.remove_from_group("shared", "bg")  # 仍在 main
            self.assertEqual(shared.disabled_groups, [])

            pool.toggle_group_disabled("shared", "main")
            pool.set_pool("shared", False)  # 整体出池
            self.assertEqual(shared.disabled_groups, [])

    def test_borrowed_subgroup_rank_follows_subgroup_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)
            pool._group_defs["bg"] = {"type": "mixed", "model": "bg"}
            self.assertTrue(pool.set_main_priority("bg", 2)[0])

            # b1 只属 bg：整组加入 main 后该名次沿用子组自身状态 → 在 bg 停用即不进 main 候选
            self.assertIn("b1", [ep.id for ep in pool._group_sticky_candidates("main")[0]])
            pool.toggle_group_disabled("b1", "bg")
            self.assertNotIn("b1", [ep.id for ep in pool._group_sticky_candidates("bg")[0]])
            self.assertNotIn("b1", [ep.id for ep in pool._group_sticky_candidates("main")[0]])
            self.assertFalse(pool._ep_routable_in_group(
                next(ep for ep in pool._endpoints if ep.id == "b1"), "main"))

            # 跨组不受约束：同属 main+bg 的原生 main 成员在 bg 停用，不影响它的 main 成员资格
            pool.toggle_group_disabled("shared", "bg")
            self.assertIn("shared", [ep.id for ep in pool._group_sticky_candidates("main")[0]])
            self.assertNotIn("shared", [ep.id for ep in pool._group_sticky_candidates("bg")[0]])

    def test_rest_route_group_scoped_vs_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, shared = self.make_pool(module)

            status, response, _ = module.api_handler(
                "POST", "/api/endpoints/shared/toggle", {"group": "bg"})
            self.assertEqual((status, response), (200, {"ok": True, "disabled": True}))
            self.assertEqual(shared.disabled_groups, ["bg"])
            self.assertTrue(shared.enabled)
            with open(module.CONFIG_FILE, encoding="utf-8") as handle:
                saved = json.load(handle)["api_endpoints"]
            self.assertEqual(next(e for e in saved if e["id"] == "shared")["disabled_groups"], ["bg"])

            status, response, _ = module.api_handler(
                "POST", "/api/endpoints/shared/toggle", {"group": "vision"})
            self.assertEqual(status, 404)

            status, response, _ = module.api_handler(
                "POST", "/api/endpoints/shared/toggle", None)
            self.assertEqual((status, response), (200, {"ok": True}))
            self.assertFalse(shared.enabled)  # 不带 group = 全局开关

    def test_config_load_normalizes_disabled_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = load_module(tmp)
            pool, _ = self.make_pool(module)
            base = {"base_url": "http://127.0.0.1:1", "api_key": "k", "model": "m",
                    "in_pool": True, "pool_groups": ["main"]}

            pool.add_endpoint(dict(base, id="ok", name="ok", disabled_groups=["main"]))
            pool.add_endpoint(dict(base, id="bad", name="bad", disabled_groups="main"))
            pool.add_endpoint(dict(base, id="none", name="none"))

            by_id = {ep.id: ep for ep in pool._endpoints}
            self.assertEqual(by_id["ok"].disabled_groups, ["main"])
            self.assertEqual(by_id["bad"].disabled_groups, [])
            self.assertEqual(by_id["none"].disabled_groups, [])


if __name__ == "__main__":
    unittest.main()
