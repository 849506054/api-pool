"""视觉池组（Vision Pool Group，2026-09-10 已拍板设计）测试。

覆盖验收矩阵：
- 内置池：vision 组恒在（次位）、名字/类型/选择器锁定、不可删除、旧配置 role 键忽略
- 零迁移：无 role 键往返持久化（was role 字段的配置直接加载）
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
    def endpoint(module, eid, is_vision=True, groups=("vision",), in_pool=True, enabled=True, priority=1):
        return module.Endpoint(
            id=eid, name=eid, base_url="http://127.0.0.1:1", api_key="k",
            model="vm", priority=priority, in_pool=in_pool, enabled=enabled,
            is_vision=is_vision, use_proxy=False, pool_groups=list(groups),
        )

    @staticmethod
    def make_pool(module, endpoints=(), groups=()):
        """建池：先建组实体再入端点（图片解析池为内置组，无需创建）。

        端点声明组名后该组名在 _all_group_names() 中即"已存在"，create_group 会拒绝；
        与真实运维顺序一致（先在 UI 建组，再放入成员）。
        """
        pool = module.APIPool([])
        for name, gtype, model in groups:
            ok, msg = pool.create_group(name, gtype, model)
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

    # ── 内置池：存在性 / 锁定 / 不可删 ──

    def test_vision_group_is_builtin(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            self.assertEqual(pool._group_defs[pool.VISION_GROUP],
                             {"type": "mixed", "model": "api-pool-vision"})
            # 全池唯一：同名组不可再创建（保留名）
            ok, msg = pool.create_group("vision", "mixed", "x")
            self.assertFalse(ok)
            self.assertIn("保留名", msg)

    def test_vision_group_name_type_selector_locked(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            for updates in ({"name": "vision2"}, {"type": "dedicated"}, {"model": "api-pool-other"}):
                ok, msg = pool.update_group("vision", updates)
                self.assertFalse(ok, f"{updates} 应被拒绝：{msg}")
            ok, _ = pool.update_group("vision", {"name": "vision", "type": "mixed",
                                                 "model": "api-pool-vision"})
            self.assertTrue(ok)

    def test_vision_group_not_deletable(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            ok, msg = pool.delete_group("vision")
            self.assertFalse(ok)
            self.assertIn("内置组", msg)

    def test_role_key_ignored_on_legacy_config_load(self):
        """旧配置的 role 键不再生效：图片解析池恒为内置 vision 组。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            pool._load_group_defs([
                {"name": "bg", "type": "mixed", "model": "api-pool-bg", "role": "vision"},
                {"name": "vision", "type": "mixed", "model": "api-pool-vision", "role": "vision"},
            ])
            self.assertNotIn("role", pool._group_defs["bg"])
            self.assertEqual(pool._group_defs["vision"], {"type": "mixed", "model": "api-pool-vision"})
            # 内置组恒在且次位：main → vision → 其余按配置顺序
            self.assertEqual(list(pool._group_defs)[:3], ["main", "vision", "bg"])

    def test_config_roundtrip_has_no_role_key_and_keeps_pool(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            module._sync_to_config()
            raw = module.load_group_defs_config()
            self.assertFalse(any("role" in g for g in raw))
            self.assertIn({"name": "vision", "type": "mixed", "model": "api-pool-vision"}, raw)
            restarted = load_module(tmp_path)
            self.assertEqual(restarted.pool._group_defs["vision"]["model"], "api-pool-vision")

    def test_api_groups_expose_is_vision_flag(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            status, resp, _ = module.api_handler("GET", "/api/groups", None)
            self.assertEqual(status, 200)
            flags = {g["name"]: g["is_vision"] for g in resp["groups"]}
            self.assertTrue(flags["vision"])
            self.assertFalse(flags["main"])
            # POST 的 role 参数已无意义：普通组照建，内置池不受影响
            status, _, _ = module.api_handler(
                "POST", "/api/groups",
                {"name": "vp", "type": "mixed", "model": "api-pool-vp", "role": "vision"},
            )
            self.assertEqual(status, 201)
            status, resp, _ = module.api_handler("GET", "/api/groups", None)
            self.assertFalse(next(g for g in resp["groups"] if g["name"] == "vp")["is_vision"])

    # ── 调度 ──

    def test_candidates_only_from_vision_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = self.make_pool(
                module,
                [self.endpoint(module, "req-vision", groups=["pool-bg"]),
                 self.endpoint(module, "pool-vision")],
                [("pool-bg", "mixed", "api-pool-bg")],
            )
            cands, grp = pool._vision_pool_candidates()
            self.assertEqual(grp, "vision")
            self.assertEqual([e.id for e in cands], ["pool-vision"])

    def test_empty_vision_pool_still_reports_group_name(self):
        """池恒存在：无成员时仍返回组名，降级文案走"无可用端点"（不再有"未配置"态）。"""
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            pool = module.APIPool([])
            self.assertEqual(pool._vision_pool_candidates(), ([], "vision"))

    def test_candidates_exclude_disabled_and_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            b1 = self.endpoint(module, "b1", enabled=True)
            b2 = self.endpoint(module, "b2", enabled=False)
            b3 = self.endpoint(module, "b3")
            pool = self.make_pool(module, [b1, b2, b3])
            b3._cooldown_until = time.time() + 60
            cands, grp = pool._vision_pool_candidates()
            self.assertEqual(grp, "vision")
            self.assertEqual([e.id for e in cands], ["b1"])

    def test_translate_uses_vision_pool_not_request_group(self):
        with tempfile.TemporaryDirectory() as tmp_path:
            module = load_module(tmp_path)
            req_vision = self.endpoint(module, "req-vision", groups=["pool-bg"])
            pool_vision = self.endpoint(module, "pool-vision")
            pool = self.make_pool(
                module, [req_vision, pool_vision],
                [("pool-bg", "mixed", "api-pool-bg")],
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
            pool = self.make_pool(module, [b1, b2])
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
            pool = self.make_pool(module, [b1, b2])
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
            pool = self.make_pool(module, [b1])
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
            pool = self.make_pool(module, [b1])
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
