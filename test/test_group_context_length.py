"""组级上下文长度（2026-09-12）测试：组声明对外窗口，Hermes 经 /v1/models 读取。

设计：
- 组实体 context_k（K=1000 tokens，0=不声明）：纯显式，不做成员推导。
- 对外暴露：GET /v1/models 每个 selector 带 context_length（tokens）；GET /v1/models/{selector} 单模型查询。
- 名称/类型/选择器锁定的内置组（main / vision）仍可改 context_k。
- 边界 2K–10000K 来自 Hermes `_coerce_reasonable_int`（1024–10,000,000 tokens）。

覆盖：显式值读取与非法存值忽略 / 未声明不外发 / 校验边界 /
内置组可改但锁定项仍拒 / 目录与单模型端点 / /api/groups 暴露 / 落盘往返。
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
        name = f"api_pool_group_context_test_{os.getpid()}_{id(threading.current_thread())}"
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


def make_endpoint(module, endpoint_id, groups, max_context_k=0, in_pool=True, enabled=True):
    return module.Endpoint(
        id=endpoint_id,
        name=endpoint_id,
        base_url="http://127.0.0.1:1",
        api_key="test",
        model="m-" + endpoint_id,
        priority=1,
        in_pool=in_pool,
        enabled=enabled,
        use_proxy=False,
        pool_groups=list(groups),
        max_context_k=max_context_k,
    )


class GroupContextLengthTests(unittest.TestCase):
    # ── 取值与推导 ──

    def test_explicit_value_read_and_invalid_ignored(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            pool._group_defs["bg"] = {"type": "mixed", "model": "api-pool-bg", "context_k": 128}
            self.assertEqual(pool._group_context_k("bg"), 128)
            self.assertEqual(pool._group_context_tokens("bg"), 128000)
            # 非法存值（越界/非数字）一律视为未声明
            pool._group_defs["bg"]["context_k"] = 1
            self.assertEqual(pool._group_context_k("bg"), 0)
            pool._group_defs["bg"]["context_k"] = "abc"
            self.assertEqual(pool._group_context_k("bg"), 0)

    def test_undeclared_group_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoints = [make_endpoint(module, "a", ["bg"], max_context_k=32)]
            pool = module.APIPool(endpoints)
            # 纯显式：成员有 max_context_k 也不推导
            self.assertIsNone(pool._group_context_tokens("bg"))
            pool._group_defs["bg"] = {"type": "mixed", "model": "api-pool-bg", "context_k": 200}
            self.assertEqual(pool._group_context_tokens("bg"), 200000)

    # ── 写入与校验 ──

    def test_create_group_validates_context_k(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            for bad in (1, 10001, -5, "x"):
                ok, msg = pool.create_group("g-%s" % bad, "mixed", "", bad)
                self.assertFalse(ok, f"context_k={bad} 应被拒")
                self.assertIn("上下文长度非法", msg)
            ok, _ = pool.create_group("bg", "mixed", "", 128)
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["context_k"], 128)
            ok, _ = pool.create_group("bg2", "mixed", "", 0)  # 0=自动，不落键
            self.assertTrue(ok)
            self.assertNotIn("context_k", pool._group_defs["bg2"])

    def test_builtin_groups_allow_context_k_but_keep_locks(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            # main：可改 context_k，名称/类型/选择器仍锁
            ok, msg = pool.update_group("main", {"type": "mixed", "model": "api-pool", "context_k": 256})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["main"]["context_k"], 256)
            self.assertFalse(pool.update_group("main", {"name": "MAIN2"})[0])
            self.assertFalse(pool.update_group("main", {"type": "dedicated"})[0])
            self.assertFalse(pool.update_group("main", {"model": "other"})[0])
            # vision：同理
            ok, msg = pool.update_group("vision", {"type": "mixed", "model": "api-pool-vision", "context_k": 64})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["vision"]["context_k"], 64)
            self.assertFalse(pool.update_group("vision", {"name": "v2"})[0])
            # 非法值拒绝；置 0 清除显式声明
            self.assertFalse(pool.update_group("main", {"context_k": 1})[0])
            ok, _ = pool.update_group("main", {"context_k": 0})
            self.assertTrue(ok)
            self.assertNotIn("context_k", pool._group_defs["main"])
            # 未传 context_k 的更新不清除既有声明
            pool.create_group("bg", "mixed", "api-pool-bg", 64)
            pool.update_group("bg", {"name": "bg", "type": "dedicated", "model": "m-x"})
            self.assertEqual(pool._group_defs["bg"]["context_k"], 64)

    # ── 对外暴露（HTTP 层）──

    def test_models_directory_exposes_context_length(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg", 128)
            module.pool.update_group("main", {"context_k": 256})
            module.pool.create_group("plain", "mixed")
            status, resp, _ = module.api_handler("GET", "/v1/models", None)
            self.assertEqual(status, 200)
            by_id = {m["id"]: m for m in resp["data"]}
            self.assertEqual(by_id["api-pool"]["context_length"], 256000)   # main 组
            self.assertEqual(by_id["api-pool-bg"]["context_length"], 128000)
            self.assertNotIn("context_length", by_id["plain"])              # 未声明 → 不带键
            self.assertNotIn("context_length", by_id["api-pool-vision"])

    def test_single_model_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg", 128)
            status, resp, _ = module.api_handler("GET", "/v1/models/api-pool-bg", None)
            self.assertEqual(status, 200)
            self.assertEqual(resp["id"], "api-pool-bg")
            self.assertEqual(resp["context_length"], 128000)
            status, resp, _ = module.api_handler("GET", "/v1/models/nope", None)
            self.assertEqual(status, 404)
            self.assertIn("not found", resp["error"]["message"])
            # 未声明窗口的选择器：仍返回模型信息，只是不带 context_length
            status, resp, _ = module.api_handler("GET", "/v1/models/api-pool-vision", None)
            self.assertEqual(status, 200)
            self.assertNotIn("context_length", resp)

    def test_groups_api_exposes_context_fields(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.add_endpoint({
                "id": "b1", "name": "b1", "base_url": "http://127.0.0.1:1", "api_key": "k",
                "model": "m1", "priority": 1, "in_pool": True, "pool_groups": ["bg"],
            })
            module.pool.create_group("bg", "mixed", "api-pool-bg")
            status, resp, _ = module.api_handler("GET", "/api/groups", None)
            self.assertEqual(status, 200)
            groups = {g["name"]: g for g in resp["groups"]}
            self.assertEqual(groups["bg"]["context_k"], 0)  # 未声明
            module.pool.update_group("bg", {"context_k": 200})
            _, resp, _ = module.api_handler("GET", "/api/groups", None)
            groups = {g["name"]: g for g in resp["groups"]}
            self.assertEqual(groups["bg"]["context_k"], 200)

    # ── 落盘往返 ──

    def test_context_k_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg", 128)
            module.pool.update_group("main", {"context_k": 256})
            module._sync_to_config()
            with open(module.CONFIG_FILE, encoding="utf-8") as handle:
                saved = json.load(handle)
            defs = {d["name"]: d for d in saved["pool_group_defs"]}
            self.assertEqual(defs["main"]["context_k"], 256)
            self.assertEqual(defs["bg"]["context_k"], 128)
            pool2 = module.APIPool([])
            pool2._load_group_defs(module.load_group_defs_config())
            self.assertEqual(pool2._group_context_k("main"), 256)
            self.assertEqual(pool2._group_context_k("bg"), 128)
            # 旧配置遗留的非法 context_k 应被忽略而非炸掉
            pool3 = module.APIPool([])
            pool3._load_group_defs([{"name": "bad", "type": "mixed", "model": "bad", "context_k": 99999}])
            self.assertEqual(pool3._group_context_k("bad"), 0)


if __name__ == "__main__":
    unittest.main()
