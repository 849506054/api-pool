"""runtime_state 事件驱动落盘（2026-09-01 方案 A）测试。

覆盖：
- _on_success 自动路由成功只更新内存态，不再热路径写盘（零 IO）
- snapshot_runtime_state 停止前快照：dump 内存指针态全量写盘，manual 优先
- 启动恢复残留自愈：组改名/删除遗留键、端点删除遗留键被清理
- 防误删：端点存在但暂不属于该组时保留键（仅 WARN）
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
        name = f"api_pool_runtime_state_test_{os.getpid()}_{id(threading.current_thread())}"
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


class RuntimeStateEventTests(unittest.TestCase):
    def endpoint(self, module, endpoint_id, priority, model, groups=None):
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

    def state_file(self, module, tmp_path):
        return os.path.join(tmp_path, module.RUNTIME_STATE_FILE)

    # ── _on_success 不再写盘 ──

    def test_on_success_updates_memory_only(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            module.pool.add_endpoint(m1)

            module.pool._on_success(m1, group="main")

            # 内存指针已更新
            self.assertEqual(module.pool._get_current("main"), "m1")
            # 热路径不再写 runtime_state 文件
            self.assertFalse(os.path.exists(self.state_file(module, tmp_path)))

    # ── 停止前快照 ──

    def test_snapshot_runtime_state_writes_all_groups_manual_first(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            b1 = self.endpoint(module, "b1", 2, "deepseek-v4-flash", groups=["bg"])
            b2 = self.endpoint(module, "b2", 3, "deepseek-v4-flash", groups=["bg"])
            module.pool.add_endpoint(m1)
            module.pool.add_endpoint(b1)
            module.pool.add_endpoint(b2)
            module.pool.create_group("bg", "mixed", "api-pool-bg")
            module.pool._set_current("main", "m1")
            module.pool._set_current("bg", "b1")
            module.pool._set_manual("bg", "b2")  # manual 优先于 current

            self.assertTrue(module.snapshot_runtime_state())

            with open(self.state_file(module, tmp_path), encoding="utf-8") as handle:
                groups = json.load(handle)["groups"]
            self.assertEqual(groups, {"main": "m1", "bg": "b2"})

    def test_snapshot_skips_groups_without_current(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            m1 = self.endpoint(module, "m1", 1, "glm-5.3")
            module.pool.add_endpoint(m1)
            module.pool.create_group("bg", "mixed", "api-pool-bg")
            module.pool._set_current("main", "m1")

            self.assertTrue(module.snapshot_runtime_state())

            with open(self.state_file(module, tmp_path), encoding="utf-8") as handle:
                groups = json.load(handle)["groups"]
            self.assertEqual(groups, {"main": "m1"})  # 无指针的组不写键

    # ── 启动恢复残留自愈 ──

    def test_startup_cleans_stale_group_name_keys(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg")
            module.pool.add_endpoint({
                "id": "b1", "name": "b1", "base_url": "http://127.0.0.1:1",
                "api_key": "test", "model": "deepseek-v4-flash", "priority": 9,
                "enabled": True, "in_pool": True, "use_proxy": False,
                "pool_groups": ["bg"],
            })
            module.pool.add_endpoint({
                "id": "m1", "name": "m1", "base_url": "http://127.0.0.1:1",
                "api_key": "test", "model": "glm-5.3", "priority": 1,
                "enabled": True, "in_pool": True, "use_proxy": False,
                "pool_groups": ["main"],
            })
            module._sync_to_config()
            # 模拟组改名遗留：api-pool-bg 是旧组名（已改名 bg），端点现属于 bg
            self.assertTrue(module.save_runtime_state_groups({
                "main": "m1",
                "bg": "b1",
                "api-pool-bg": "b1",
            }))

            restarted = load_module(tmp_path)

            # 有效键恢复成功
            self.assertEqual(restarted.pool._get_current("main"), "m1")
            self.assertEqual(restarted.pool._get_current("bg"), "b1")
            # 残留旧组名键已被清理
            with open(self.state_file(restarted, tmp_path), encoding="utf-8") as handle:
                groups = json.load(handle)["groups"]
            self.assertEqual(groups, {"main": "m1", "bg": "b1"})

    def test_startup_cleans_missing_endpoint_keys(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.add_endpoint({
                "id": "m1", "name": "m1", "base_url": "http://127.0.0.1:1",
                "api_key": "test", "model": "glm-5.3", "priority": 1,
                "enabled": True, "in_pool": True, "use_proxy": False,
                "pool_groups": ["main"],
            })
            module._sync_to_config()
            # 模拟端点删除遗留：ghost 端点已不存在
            self.assertTrue(module.save_runtime_state_groups({
                "main": "m1",
                "main-ghost": "no_such_endpoint_id",
            }))

            restarted = load_module(tmp_path)

            self.assertEqual(restarted.pool._get_current("main"), "m1")
            with open(self.state_file(restarted, tmp_path), encoding="utf-8") as handle:
                groups = json.load(handle)["groups"]
            self.assertEqual(groups, {"main": "m1"})

    def test_startup_keeps_endpoint_with_group_mismatch(self):
        """端点存在但暂不属于该组：恢复失败仅 WARN，不删键（防误删合法指针）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("g1", "mixed", "g1")
            module.pool.add_endpoint({
                "id": "e1", "name": "e1", "base_url": "http://127.0.0.1:1",
                "api_key": "test", "model": "glm-5.3", "priority": 1,
                "enabled": True, "in_pool": True, "use_proxy": False,
                "pool_groups": ["main"],  # e1 属于 main，不属于 g1
            })
            module._sync_to_config()
            self.assertTrue(module.save_runtime_state_groups({"g1": "e1"}))

            restarted = load_module(tmp_path)

            # g1 组存在、e1 端点存在：键保留（仅 WARN）
            self.assertIsNone(restarted.pool._get_current("g1"))
            with open(self.state_file(restarted, tmp_path), encoding="utf-8") as handle:
                groups = json.load(handle)["groups"]
            self.assertEqual(groups, {"g1": "e1"})


if __name__ == "__main__":
    unittest.main()
