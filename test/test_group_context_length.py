"""组级上下文长度测试（2026-09-13：单位 K → tokens 精确值）。

设计：
- 组实体 context_tokens（tokens，0=不声明）：纯显式，不做成员推导；存各家模型的真实窗口（如 1048576 / 202752）。
- 对外暴露：GET /v1/models 每个 selector 带 context_length（tokens）；GET /v1/models/{selector} 单模型查询。
- 名称/类型/选择器锁定的内置组（main / vision）仍可改 context_tokens。
- 边界 2,000–10,000,000 tokens 来自 Hermes `_coerce_reasonable_int`（1024–10,000,000 tokens）。
- 旧配置 context_k（K=1000 tokens）读入时自动换算为 tokens。

覆盖：显式值读取与非法存值忽略 / 未声明不外发 / 校验边界 / 精确值不被取整 /
内置组可改但锁定项仍拒 / 目录与单模型端点 / /api/groups 暴露 / 落盘往返 / 旧键兼容。
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
            pool._group_defs["bg"] = {"type": "mixed", "model": "api-pool-bg", "context_tokens": 202752}
            self.assertEqual(pool._group_context_tokens("bg"), 202752)
            self.assertEqual(pool._group_defs["bg"]["context_tokens"], 202752)
            # 非法存值（越界/非数字）一律视为未声明
            pool._group_defs["bg"]["context_tokens"] = 1000
            self.assertIsNone(pool._group_context_tokens("bg"))
            pool._group_defs["bg"]["context_tokens"] = "abc"
            self.assertIsNone(pool._group_context_tokens("bg"))

    def test_undeclared_group_is_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            endpoints = [make_endpoint(module, "a", ["bg"], max_context_k=32)]
            pool = module.APIPool(endpoints)
            # 纯显式：成员有 max_context_k 也不推导
            self.assertIsNone(pool._group_context_tokens("bg"))
            pool._group_defs["bg"] = {"type": "mixed", "model": "api-pool-bg", "context_tokens": 200000}
            self.assertEqual(pool._group_context_tokens("bg"), 200000)

    # ── 写入与校验 ──

    def test_create_group_validates_context_tokens(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            for bad in (1000, 10000001, -5, "x"):
                ok, msg = pool.create_group("g-%s" % bad, "mixed", "", bad)
                self.assertFalse(ok, f"context_tokens={bad} 应被拒")
                self.assertIn("上下文长度非法", msg)
            ok, _ = pool.create_group("bg", "mixed", "", 1048576)
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["context_tokens"], 1048576)  # 精确值不取整
            ok, _ = pool.create_group("bg2", "mixed", "", 0)  # 0=不声明，不落键
            self.assertTrue(ok)
            self.assertNotIn("context_tokens", pool._group_defs["bg2"])

    def test_builtin_groups_allow_context_tokens_but_keep_locks(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            # main：可改 context_tokens，名称/类型/选择器仍锁
            ok, msg = pool.update_group("main", {"type": "mixed", "model": "api-pool", "context_tokens": 1000000})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["main"]["context_tokens"], 1000000)
            self.assertFalse(pool.update_group("main", {"name": "MAIN2"})[0])
            self.assertFalse(pool.update_group("main", {"type": "dedicated"})[0])
            self.assertFalse(pool.update_group("main", {"model": "other"})[0])
            # vision：同理
            ok, msg = pool.update_group("vision", {"type": "mixed", "model": "api-pool-vision", "context_tokens": 65536})
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_defs["vision"]["context_tokens"], 65536)
            self.assertFalse(pool.update_group("vision", {"name": "v2"})[0])
            # 非法值拒绝；置 0 清除显式声明
            self.assertFalse(pool.update_group("main", {"context_tokens": 1000})[0])
            ok, _ = pool.update_group("main", {"context_tokens": 0})
            self.assertTrue(ok)
            self.assertNotIn("context_tokens", pool._group_defs["main"])
            # 未传 context_tokens 的更新不清除既有声明
            pool.create_group("bg", "mixed", "api-pool-bg", 1048576)
            pool.update_group("bg", {"name": "bg", "type": "dedicated", "model": "m-x"})
            self.assertEqual(pool._group_defs["bg"]["context_tokens"], 1048576)

    # ── 对外暴露（HTTP 层）──

    def test_models_directory_exposes_context_length(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg", 1048576)
            module.pool.update_group("main", {"context_tokens": 200000})
            module.pool.create_group("plain", "mixed")
            status, resp, _ = module.api_handler("GET", "/v1/models", None)
            self.assertEqual(status, 200)
            by_id = {m["id"]: m for m in resp["data"]}
            self.assertEqual(by_id["api-pool"]["context_length"], 200000)      # main 组
            self.assertEqual(by_id["api-pool-bg"]["context_length"], 1048576)  # 精确 1Mi
            self.assertNotIn("context_length", by_id["plain"])                 # 未声明 → 不带键
            self.assertNotIn("context_length", by_id["api-pool-vision"])

    def test_single_model_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg", 128000)
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
            self.assertEqual(groups["bg"]["context_tokens"], 0)  # 未声明
            module.pool.update_group("bg", {"context_tokens": 202752})
            _, resp, _ = module.api_handler("GET", "/api/groups", None)
            groups = {g["name"]: g for g in resp["groups"]}
            self.assertEqual(groups["bg"]["context_tokens"], 202752)

    def test_groups_post_accepts_context_tokens(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            status, resp, _ = module.api_handler(
                "POST", "/api/groups", {"name": "bg", "type": "mixed", "model": "api-pool-bg", "context_tokens": 1048576})
            self.assertEqual(status, 201, resp)
            self.assertEqual(module.pool._group_defs["bg"]["context_tokens"], 1048576)

    # ── 落盘往返 ──

    def test_context_tokens_persists_and_reloads(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("bg", "mixed", "api-pool-bg", 1048576)
            module.pool.update_group("main", {"context_tokens": 200000})
            module._sync_to_config()
            with open(module.CONFIG_FILE, encoding="utf-8") as handle:
                saved = json.load(handle)
            defs = {d["name"]: d for d in saved["pool_group_defs"]}
            self.assertEqual(defs["main"]["context_tokens"], 200000)
            self.assertEqual(defs["bg"]["context_tokens"], 1048576)
            pool2 = module.APIPool([])
            pool2._load_group_defs(module.load_group_defs_config())
            self.assertEqual(pool2._group_context_tokens("main"), 200000)
            self.assertEqual(pool2._group_context_tokens("bg"), 1048576)
            # 旧配置遗留的非法值应被忽略而非炸掉
            pool3 = module.APIPool([])
            pool3._load_group_defs([{"name": "bad", "type": "mixed", "model": "bad", "context_tokens": 99999999}])
            self.assertEqual(pool3._group_context_tokens("bad"), None)

    def test_legacy_context_k_migrates_to_tokens(self):
        """旧配置 context_k（K=1000 tokens）读入 → context_tokens；main / 普通组都覆盖。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            pool._load_group_defs([
                {"name": "main", "type": "mixed", "model": "api-pool", "context_k": 1000},
                {"name": "legacy", "type": "mixed", "model": "legacy", "context_k": 128},
            ])
            self.assertEqual(pool._group_context_tokens("main"), 1000000)
            self.assertEqual(pool._group_context_tokens("legacy"), 128000)
            # context_tokens 优先于遗留 context_k
            pool2 = module.APIPool([])
            pool2._load_group_defs([{"name": "both", "type": "mixed", "model": "both",
                                     "context_k": 128, "context_tokens": 1048576}])
            self.assertEqual(pool2._group_context_tokens("both"), 1048576)


if __name__ == "__main__":
    unittest.main()
