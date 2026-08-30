"""组管理（2026-08-30）E2E 测试：组实体 CRUD、selector 路由、dedicated 校验、持久化。

覆盖：
- create_group：合法/重名/保留名/dedicated 缺模型/选择器冲突
- update_group：改名（端点/指针同步）、mixed→dedicated 成员模型校验、main 锁定
- delete_group：成员移出、指针清理、main 拒删
- _resolve_request_group：selector 优先解析（mixed=Hermes 配置名 / dedicated=真实模型名）
- set_pool 入组校验：dedicated 模型不匹配被过滤
- update_endpoint 改模型：所属 dedicated 组自动移出
- 持久化：pool_group_defs 落盘与加载往返
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
        name = f"api_pool_group_mgmt_test_{os.getpid()}_{id(threading.current_thread())}"
        spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load api_pool_server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        os.chdir(previous_cwd)


class GroupManagementTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, endpoint_id, priority, model, groups=None):
        return module.Endpoint(
            id=endpoint_id,
            name=endpoint_id,
            base_url="http://127.0.0.1:1",
            api_key="test",
            model=model,
            priority=priority,
            in_pool=True,
            use_proxy=False,
            pool_groups=groups or ["main"],
        )

    def make_pool(self, module, endpoints):
        return module.APIPool(endpoints)

    # ── 创建 ──

    def test_create_mixed_group_defaults_selector_to_name(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            ok, msg = pool.create_group("bg", "mixed", "")
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["bg"], {"type": "mixed", "model": "bg"})

    def test_create_dedicated_requires_model(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            ok, msg = pool.create_group("ds", "dedicated", "")
            self.assertFalse(ok)
            self.assertIn("绑定模型", msg)

    def test_create_rejects_duplicate_and_reserved(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            ok, _ = pool.create_group("bg", "mixed", "")
            self.assertTrue(ok)
            ok, msg = pool.create_group("bg", "mixed", "")
            self.assertFalse(ok)
            ok, msg = pool.create_group("main", "mixed", "")
            self.assertFalse(ok)
            ok, msg = pool.create_group("api-pool", "mixed", "")
            self.assertFalse(ok)

    def test_create_rejects_selector_conflict(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            ok, _ = pool.create_group("bg", "mixed", "api-pool-bg")
            self.assertTrue(ok)
            ok, msg = pool.create_group("other", "mixed", "api-pool-bg")
            self.assertFalse(ok)
            self.assertIn("选择器", msg)

    # ── 编辑 ──

    def test_rename_syncs_endpoints_and_pointers(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "m1", groups=["bg", "main"])
            pool = self.make_pool(module, [ep])
            # 端点声明使 bg 已隐式存在（无实体）→ 先派生实体再改名
            pool._derive_group_defs()
            pool._set_current("bg", "b1")
            pool._set_manual("bg", "b1")
            ok, msg = pool.update_group("bg", {"name": "bg2"})
            self.assertTrue(ok, msg)
            self.assertEqual(ep.pool_groups, ["bg2", "main"])
            self.assertEqual(pool._current_endpoint_by_group.get("bg2"), "b1")
            self.assertNotIn("bg", pool._current_endpoint_by_group)
            self.assertEqual(pool._group_defs["bg2"]["model"], "bg2")  # 派生选择器跟随新名

    def test_update_selector_keeps_name(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "m1", groups=["bg", "main"])
            pool = self.make_pool(module, [ep])
            ok, msg = pool.update_group("bg", {"model": "api-pool-bg"})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["bg"]["model"], "api-pool-bg")
            # 组名与选择器分离：两个入口都能路由到 bg
            self.assertEqual(pool._resolve_request_group("bg"), "bg")
            self.assertEqual(pool._resolve_request_group("api-pool-bg"), "bg")

    def test_mixed_to_dedicated_rejects_mismatched_members(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "glm-5.3", groups=["bg", "main"])
            pool = self.make_pool(module, [ep])
            ok, msg = pool.update_group("bg", {"type": "dedicated", "model": "deepseek-v4-flash"})
            self.assertFalse(ok)
            self.assertIn("不匹配", msg)
            # 全员匹配则通过
            ep.model = "deepseek-v4-flash"
            ok, msg = pool.update_group("bg", {"type": "dedicated", "model": "deepseek-v4-flash"})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["bg"]["type"], "dedicated")

    def test_main_group_locked(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            ok, msg = pool.update_group("main", {"name": "root"})
            self.assertFalse(ok)
            ok, msg = pool.update_group("main", {"type": "dedicated", "model": "x"})
            self.assertFalse(ok)
            ok, msg = pool.update_group("main", {"model": "other"})
            self.assertFalse(ok)

    # ── 删除 ──

    def test_delete_group_evicts_members(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep1 = self.endpoint(module, "b1", 1, "m1", groups=["bg", "main"])
            ep2 = self.endpoint(module, "b2", 2, "m2", groups=["bg"])  # bg 是唯一组
            pool = self.make_pool(module, [ep1, ep2])
            pool._set_current("bg", "b1")
            ok, msg = pool.delete_group("bg")
            self.assertTrue(ok, msg)
            self.assertEqual(ep1.pool_groups, ["main"])  # 仍有 main，只是移出 bg
            self.assertFalse(ep2.in_pool)  # 最后一组 → 整体出池
            self.assertNotIn("bg", pool._current_endpoint_by_group)

    def test_delete_main_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            ok, msg = pool.delete_group("main")
            self.assertFalse(ok)

    # ── 路由解析（selector 优先）──

    def test_selector_resolution_mixed_and_dedicated(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "deepseek-v4-flash", groups=["bg", "main"])
            pool = self.make_pool(module, [ep])
            pool._derive_group_defs()  # bg 从端点声明派生
            ok, _ = pool.update_group("bg", {"model": "api-pool-bg"})  # bg 已存在 → 编辑选择器
            self.assertTrue(ok)
            pool.create_group("ds", "dedicated", "deepseek-v4-flash")
            self.assertEqual(pool._resolve_request_group("api-pool-bg"), "bg")
            self.assertEqual(pool._resolve_request_group("bg"), "bg")  # 组名仍可解析
            self.assertEqual(pool._resolve_request_group("deepseek-v4-flash"), "ds")
            self.assertEqual(pool._resolve_request_group("api-pool"), "main")
            self.assertEqual(pool._resolve_request_group("unknown"), "main")

    # ── 入组校验 ──

    def test_set_pool_filters_dedicated_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "glm-5.3", groups=["main"])
            pool = self.make_pool(module, [ep])
            pool.create_group("ds", "dedicated", "deepseek-v4-flash")
            pool.set_pool("b1", True, groups=["ds", "main"])  # ds 模型不匹配
            self.assertEqual(ep.pool_groups, ["main"])

    def test_endpoint_model_change_evicts_dedicated_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "deepseek-v4-flash", groups=["ds", "main"])
            pool = self.make_pool(module, [ep])
            pool._load_group_defs([
                {"name": "ds", "type": "dedicated", "model": "deepseek-v4-flash"},
            ])
            pool.update_endpoint("b1", {"model": "glm-5.3"})
            self.assertEqual(ep.pool_groups, ["main"])  # ds 自动移出，main 保留

    # ── 持久化往返 ──

    def test_defs_persist_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(module, [])
            pool.create_group("bg", "mixed", "api-pool-bg")
            pool.create_group("ds", "dedicated", "deepseek-v4-flash")
            # 模拟 _sync_to_config 的 defs 序列化
            defs_list = [{"name": pool.MAIN_GROUP, **pool._group_defs[pool.MAIN_GROUP]}]
            for gname, gd in pool._group_defs.items():
                if gname != pool.MAIN_GROUP:
                    defs_list.append({"name": gname, "type": gd["type"], "model": gd["model"]})
            module.save_config([], group_defs=defs_list)
            # 重新加载
            raw = module.load_group_defs_config()
            pool2 = self.make_pool(module, [])
            pool2._load_group_defs(raw)
            self.assertEqual(pool2._group_defs["bg"], {"type": "mixed", "model": "api-pool-bg"})
            self.assertEqual(pool2._group_defs["ds"], {"type": "dedicated", "model": "deepseek-v4-flash"})
            self.assertEqual(pool2._group_defs["main"]["model"], "api-pool")

    def test_legacy_config_derives_defs_without_persist(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "m1", groups=["bg", "main"])
            pool = self.make_pool(module, [ep])
            # 旧配置：无 pool_group_defs → 派生（selector=组名，mixed）
            pool._derive_group_defs()
            self.assertEqual(pool._group_defs["bg"], {"type": "mixed", "model": "bg"})
            self.assertEqual(pool._resolve_request_group("bg"), "bg")
            # 不带 defs 保存（旧路径）→ defs 不落盘
            module.save_config([])
            self.assertIsNone(module.load_group_defs_config())

    def test_models_directory_lists_selectors(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            ep = self.endpoint(module, "b1", 1, "deepseek-v4-flash", groups=["bg", "main"])
            pool = self.make_pool(module, [ep])
            pool._derive_group_defs()
            ok, _ = pool.update_group("bg", {"model": "api-pool-bg"})
            self.assertTrue(ok)
            pool.create_group("ds", "dedicated", "deepseek-v4-flash")
            # api_handler 走模块级 pool → 把测试组同步过去（选 defs + 指针态让组可见）
            module.pool._group_defs = dict(pool._group_defs)
            for grp in ("bg", "ds"):
                module.pool._set_current(grp, "nonexistent")  # 仅让组名进入指针态集合
            status, resp, _ = module.api_handler("GET", "/v1/models", None)
            ids = [m["id"] for m in resp["data"]]
            self.assertEqual(status, 200)
            self.assertIn("api-pool", ids)
            self.assertIn("api-pool-bg", ids)  # bg 组 selector（非组名）
            self.assertIn("deepseek-v4-flash", ids)  # dedicated 组 selector


if __name__ == "__main__":
    unittest.main()
