"""视觉池组（Vision Pool Group，2026-09-10 已拍板设计）测试。

覆盖验收矩阵：
- role CRUD：创建 vision 组、全池唯一（create/update 拒绝第二个）、非法值拒绝、main 锁定
- 零迁移：旧配置无 role 键加载为普通组；role 随配置往返持久化
- 调度：_vision_pool_candidates 只从视觉池取（请求组内 is_vision 端点不再入选）
- 降级：无视觉池 → 原样返回不翻译；翻译全失败 → 原样返回
- 冷却：成员翻译失败写阶梯短冷却并排除后续候选；成功清冷却
- 缓存：同批图片命中短 TTL 缓存跳过二次转译；过期失效
"""

import importlib.util
import base64
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib

MODULE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "api_pool_server.py")


def load_module(tmp_path):
    previous_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        name = f"api_pool_vision_pool_test_{os.getpid()}_{id(threading.current_thread())}"
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


class VisionPoolGroupTests(unittest.TestCase):
    @staticmethod
    def endpoint(module, eid, is_vision=True, groups=("vision-pool",), in_pool=True, enabled=True, priority=1):
        return module.Endpoint(
            id=eid, name=eid, base_url="http://127.0.0.1:1", api_key="k",
            model="vm", priority=priority, in_pool=in_pool, enabled=enabled,
            is_vision=is_vision, use_proxy=False, pool_groups=list(groups),
        )

    @staticmethod
    def make_pool(module, endpoints=(), groups=()):
        """建池：先建组实体再入端点。

        端点声明组名后该组名在 _all_group_names() 中即"已存在"，create_group 会拒绝；
        与真实运维顺序一致（先在 UI 建组，再放入成员）。
        """
        pool = module.APIPool([])
        for name, gtype, model, role in groups:
            ok, msg = pool.create_group(name, gtype, model, role)
            assert ok, msg
        for ep in endpoints:
            pool.add_endpoint(ep)
        return pool

    @staticmethod
    def image_message(url):
        return [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": url}}]}]

    @staticmethod
    def ok_result(*_args, **_kwargs):
        return {"choices": [{"message": {"content": "desc"}}]}, ""

    # ── role CRUD ──

    def test_create_vision_group_and_uniqueness(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            ok, msg = pool.create_group("vision", "mixed", "api-pool-vision", "vision")
            self.assertTrue(ok, msg)
            self.assertEqual(pool._vision_group_name(), "vision")
            ok, msg = pool.create_group("vision2", "mixed", "x", "vision")
            self.assertFalse(ok)
            self.assertIn("已存在", msg)

    def test_update_to_vision_rejected_when_exists(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            pool.create_group("vision", "mixed", "api-pool-vision", "vision")
            pool.create_group("bg", "mixed", "api-pool-bg")
            ok, msg = pool.update_group("bg", {"role": "vision"})
            self.assertFalse(ok)
            self.assertIn("已存在", msg)

    def test_invalid_role_rejected(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            ok, msg = pool.create_group("g", "mixed", "", "image")
            self.assertFalse(ok)
            pool.create_group("bg", "mixed", "")
            ok, msg = pool.update_group("bg", {"role": "bogus"})
            self.assertFalse(ok)

    def test_main_cannot_be_vision(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            ok, msg = pool.update_group("main", {"role": "vision"})
            self.assertFalse(ok)

    def test_legacy_config_without_role_loads_normal(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            pool._load_group_defs([{"name": "bg", "type": "mixed", "model": "api-pool-bg"}])
            self.assertNotIn("role", pool._group_defs["bg"])
            self.assertIsNone(pool._vision_group_name())
            pool._load_group_defs([{"name": "vision", "type": "mixed", "model": "api-pool-vision", "role": "vision"}])
            self.assertEqual(pool._vision_group_name(), "vision")

    def test_vision_role_roundtrip_persists(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module.pool.create_group("vision", "mixed", "api-pool-vision", "vision")
            module._sync_to_config()
            raw = module.load_group_defs_config()
            self.assertTrue(any(g.get("role") == "vision" for g in raw))
            restarted = load_module(tmp_path)
            self.assertEqual(restarted.pool._vision_group_name(), "vision")

    def test_api_groups_expose_and_accept_role(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            status, resp, _ = module.api_handler(
                "POST", "/api/groups",
                {"name": "vp", "type": "mixed", "model": "api-pool-vision", "role": "vision"},
            )
            self.assertEqual(status, 201)
            status, resp, _ = module.api_handler("GET", "/api/groups", None)
            g = next(x for x in resp["groups"] if x["name"] == "vp")
            self.assertEqual(g["role"], "vision")

    # ── 调度 ──

    def test_candidates_only_from_vision_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(
                module,
                [self.endpoint(module, "req-vision", groups=["pool-bg"]),
                 self.endpoint(module, "pool-vision")],
                [("pool-bg", "mixed", "api-pool-bg", ""),
                 ("vision-pool", "mixed", "api-pool-vision", "vision")],
            )
            cands, grp = pool._vision_pool_candidates()
            self.assertEqual(grp, "vision-pool")
            self.assertEqual([e.id for e in cands], ["pool-vision"])

    def test_candidates_exclude_disabled_and_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", enabled=True)
            b2 = self.endpoint(module, "b2", enabled=False)
            b3 = self.endpoint(module, "b3")
            pool = self.make_pool(module, [b1, b2, b3],
                                  [("vision-pool", "mixed", "api-pool-vision", "vision")])
            b3._cooldown_until = time.time() + 60
            cands, grp = pool._vision_pool_candidates()
            self.assertEqual(grp, "vision-pool")
            self.assertEqual([e.id for e in cands], ["b1"])

    def test_translate_uses_vision_pool_not_request_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            req_vision = self.endpoint(module, "req-vision", groups=["pool-bg"])
            pool_vision = self.endpoint(module, "pool-vision")
            pool = self.make_pool(
                module, [req_vision, pool_vision],
                [("pool-bg", "mixed", "api-pool-bg", ""),
                 ("vision-pool", "mixed", "api-pool-vision", "vision")],
            )
            calls = []

            def fake_try(ep, *_args, **_kwargs):
                calls.append(ep.id)
                return {"choices": [{"message": {"content": "desc"}}]}, ""

            pool._try_endpoint = fake_try
            translated = pool._translate_images_sync(
                self.image_message("data:x"), [req_vision], "pool-bg",
            )
            self.assertEqual(calls, ["pool-vision"])
            self.assertIn("图片解析内容", translated[0]["content"][-1]["text"])

    def test_translate_without_pool_returns_original(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            calls = []
            pool._try_endpoint = lambda *a, **k: calls.append(1) or self.ok_result()
            msgs = self.image_message("data:x")
            out = pool._translate_images_sync(msgs, [])
            self.assertIs(out, msgs)
            self.assertEqual(calls, [])

    def test_translate_all_fail_returns_original(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", priority=1)
            b2 = self.endpoint(module, "b2", priority=2)
            pool = self.make_pool(module, [b1, b2],
                                  [("vision-pool", "mixed", "api-pool-vision", "vision")])
            pool._try_endpoint = lambda *_a, **_k: (None, "HTTP 500")
            msgs = self.image_message("data:x")
            out = pool._translate_images_sync(msgs, [b1, b2])
            self.assertIs(out, msgs)

    # ── 冷却 ──

    def test_translate_failure_sets_cooldown_next_skips(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", priority=1)
            b2 = self.endpoint(module, "b2", priority=2)
            pool = self.make_pool(module, [b1, b2],
                                  [("vision-pool", "mixed", "api-pool-vision", "vision")])
            calls = []

            def fake_try(ep, *_a, **_k):
                calls.append(ep.id)
                if ep.id == "b1":
                    return None, "HTTP 500"
                return self.ok_result()

            pool._try_endpoint = fake_try
            translated = pool._translate_images_sync(self.image_message("data:x1"), [b1, b2])
            self.assertIn("图片解析内容", translated[0]["content"][-1]["text"])
            self.assertEqual(calls, ["b1", "b2"])
            self.assertTrue(b1._cooldown_until > time.time())
            self.assertEqual(b1._cooldown_reason, "vision_translate_failed")
            self.assertEqual(b1._fail_count, 1)
            # 第二次：b1 已被冷却排除，候选直接从 b2 开始
            calls.clear()
            pool._translate_images_sync(self.image_message("data:x2"), [b1, b2])
            self.assertEqual(calls, ["b2"])

    def test_translate_success_clears_fail_state(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1")
            pool = self.make_pool(module, [b1],
                                  [("vision-pool", "mixed", "api-pool-vision", "vision")])
            b1._fail_count = 3
            b1._cooldown_reason = "vision_translate_failed"
            pool._try_endpoint = self.ok_result
            pool._translate_images_sync(self.image_message("data:x"), [b1])
            self.assertEqual(b1._fail_count, 0)
            self.assertEqual(b1._cooldown_reason, "")

    # ── 缓存 ──

    def test_cache_hit_skips_second_translate(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1")
            pool = self.make_pool(module, [b1],
                                  [("vision-pool", "mixed", "api-pool-vision", "vision")])
            calls = []
            pool._try_endpoint = lambda *_a, **_k: calls.append(1) or self.ok_result()
            msgs = self.image_message("data:x")
            out1 = pool._translate_images_sync(msgs, [b1])
            out2 = pool._translate_images_sync(msgs, [b1])
            self.assertIn("图片解析内容", out1[0]["content"][-1]["text"])
            self.assertIn("图片解析内容", out2[0]["content"][-1]["text"])
            self.assertEqual(len(calls), 1)

    def test_cache_expiry(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            pool._vision_cache["k"] = (time.time() - 400, "old")
            self.assertIsNone(pool._vision_cache_get("k"))
            self.assertNotIn("k", pool._vision_cache)

    def test_vision_cache_key_semantics(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            m_ab = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "a"}},
                {"type": "image_url", "image_url": {"url": "b"}},
            ]}]
            m_ab_again = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "a"}},
                {"type": "image_url", "image_url": {"url": "b"}},
            ]}]
            m_ba = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": "b"}},
                {"type": "image_url", "image_url": {"url": "a"}},
            ]}]
            m_c = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "c"}}]}]
            # 同一批图片（有序签名）→ 同 key；顺序变化/换图 → 不同 key；无图 → None
            self.assertEqual(pool._vision_cache_key(m_ab), pool._vision_cache_key(m_ab_again))
            self.assertNotEqual(pool._vision_cache_key(m_ab), pool._vision_cache_key(m_ba))
            self.assertNotEqual(pool._vision_cache_key(m_ab), pool._vision_cache_key(m_c))
            self.assertIsNone(pool._vision_cache_key([{"role": "user", "content": "text"}]))


    # ── 入池前探测夹具 ──

    def test_vision_probe_fixture_is_valid_png(self):
        """test-vision 探测图必须是合法且 >28px 的 PNG：Qwen3-VL 拒 1x1，SiliconFlow 校 IDAT。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            raw = base64.b64decode(module.APIPool()._tiny_png_base64())
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            width, height = struct.unpack(">II", raw[16:24])
            self.assertGreater(width, 28)
            self.assertGreater(height, 28)
            decompressor = zlib.decompressobj()
            self.assertTrue(decompressor.decompress(raw[raw.index(b"IDAT") + 4:]))


if __name__ == "__main__":
    unittest.main()
