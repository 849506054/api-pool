"""组级端点轮换 rotate_requests（2026-10-05）：按成功请求次数达标推进当前端点。

口径：0=不轮换；>0 时每成功 N 次请求把本组当前端点按组内优先级推进到下一名（循环）。
手动切换是那一刻的用户动作，组配了轮换就按配置继续推进（轮换不因 manual 暂停）。
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
            self.assertEqual(pool._valid_group_rotate_requests(0), 0)
            self.assertEqual(pool._valid_group_rotate_requests(None), 0)
            self.assertEqual(pool._valid_group_rotate_requests("20"), 20)
            self.assertEqual(pool._valid_group_rotate_requests(1), 1)
            self.assertEqual(pool._valid_group_rotate_requests(100000), 100000)
            self.assertIsNone(pool._valid_group_rotate_requests(-1))
            self.assertIsNone(pool._valid_group_rotate_requests(100001))
            self.assertIsNone(pool._valid_group_rotate_requests("abc"))

    def test_default_zero_does_not_store_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            ok, msg = pool.create_group("bg", "mixed", "", 0, 0, "all", 0)
            self.assertTrue(ok, msg)
            self.assertNotIn("rotate_requests", pool._group_defs["bg"])

    def test_create_and_update_store_rotate_requests(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            ok, _ = pool.create_group("bg", "mixed", "", 0, 0, "all", 5)
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["rotate_requests"], 5)
            self.assertEqual(pool._group_rotate_requests("bg"), 5)
            # 非法拒绝
            ok, msg = pool.create_group("bad", "mixed", "", 0, 0, "all", 999999)
            self.assertFalse(ok)
            self.assertIn("轮换", msg)
            # 编辑更新与清零（0 → 不落键）
            ok, _ = pool.update_group("bg", {"rotate_requests": 10})
            self.assertTrue(ok)
            self.assertEqual(pool._group_defs["bg"]["rotate_requests"], 10)
            ok, _ = pool.update_group("bg", {"rotate_requests": 0})
            self.assertTrue(ok)
            self.assertNotIn("rotate_requests", pool._group_defs["bg"])

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

    def test_rotate_continues_after_manual_switch(self):
        """A 口径（2026-10-05）：手动切换后一样按配置轮换；轮换同时清除组内锁定，让新指针真的生效。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_current("bg", "b1")
            pool._set_manual("bg", "b3")           # 用户此刻手动切到 b3（或开机恢复钉住）
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b2")  # 到点照样推进
            self.assertIsNone(pool._get_manual("bg"), "轮换后不残留组内锁定，否则路由仍走锁定端点")

    def test_rotate_clears_boot_restore_pin(self):
        """开机恢复把组钉在端点 A；轮换后路由必须落到新指针（不被恢复到的手动锁定回钉）。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_current("bg", "b1")
            pool._set_manual("bg", "b1")           # 恢复态：manual == current == b1
            pool._rotate_group_once("bg")
            # 下一次请求的指针取值顺序：manual 优先，其次 current
            self.assertEqual(pool._get_manual("bg") or pool._get_current("bg"), "b2")

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

    def test_count_fires_at_threshold_and_resets(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_group_rotate_requests("bg", 3)
            pool._set_current("bg", "b1")
            pool._count_rotate_request("bg")
            pool._count_rotate_request("bg")
            self.assertEqual(pool._get_current("bg"), "b1")   # 未达阈值不动
            pool._count_rotate_request("bg")
            self.assertEqual(pool._get_current("bg"), "b2")   # 第 3 次 → 推进
            self.assertEqual(pool._rotate_counts["bg"], 0)    # 计数重新累计
            # 未配置轮换的组：不计数、不动
            pool._count_rotate_request("main")
            self.assertNotIn("main", pool._rotate_counts)

    def test_on_success_counts_toward_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, eps = self.pool_with_bg(m)
            pool._set_group_rotate_requests("bg", 2)
            pool._set_current("bg", "b1")
            pool._on_success(eps[0], group="bg")
            self.assertEqual(pool._get_current("bg"), "b1")
            pool._on_success(eps[0], group="bg")
            self.assertEqual(pool._get_current("bg"), "b2")

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "", 0, 0, "all", 15)
            m._sync_to_config()
            saved = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
            entry = next(d for d in saved["pool_group_defs"] if d["name"] == "bg")
            self.assertEqual(entry["rotate_requests"], 15)
            raw = m.load_group_defs_config()
            pool2 = m.APIPool([])
            pool2._load_group_defs(raw)
            self.assertEqual(pool2._group_rotate_requests("bg"), 15)
            # all 默认态：0 不落键、读回 0
            self.assertEqual(pool2._group_rotate_requests("main"), 0)

    def test_legacy_rotate_minutes_cleared_not_migrated(self):
        """旧分钟制键（2026-10-05 前）不迁移：读入当未声明，落盘清除。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            with open(m.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"endpoints": [], "pool_group_defs": [
                    {"name": "main", "type": "mixed", "model": "main"},
                    {"name": "bg", "type": "mixed", "model": "bg", "rotate_minutes": 30},
                ]}, f)
            m.pool._load_group_defs(m.load_group_defs_config())
            self.assertEqual(m.pool._group_rotate_requests("bg"), 0)
            m._sync_to_config()
            saved = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
            entry = next(d for d in saved["pool_group_defs"] if d["name"] == "bg")
            self.assertNotIn("rotate_minutes", entry)


    def test_rotate_members_subset_advances_within_selection(self):
        """选择性轮换（2026-10-06）：只在选中成员之间推进，未选中的成员被跳过。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_group_rotate_members("bg", ["b1", "b3"])
            pool._set_current("bg", "b1")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b3")   # b2 不在选中集，跳过
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b1")   # 选中集内循环

    def test_rotate_members_empty_selection_means_all(self):
        """不选任何成员 = 全体参与（现状行为零回归）。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            self.assertEqual(pool._group_rotate_members("bg"), [])
            pool._set_current("bg", "b1")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b2")

    def test_rotate_members_selection_all_unavailable_noop(self):
        """选中成员当前全不可用 → 本轮不轮换，且不回落全体成员。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, eps = self.pool_with_bg(m)
            pool._set_group_rotate_members("bg", ["b2", "b3"])
            pool._set_current("bg", "b1")
            eps[1]._cooldown_until = m.time.time() + 600
            eps[2]._cooldown_until = m.time.time() + 600
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b1")   # 不落回 b1 之外的全体成员

    def test_rotate_members_single_selection_converges(self):
        """只选 1 个成员：下一次轮换切到它并停住（已在其上则无动作）。"""
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool, _ = self.pool_with_bg(m)
            pool._set_group_rotate_members("bg", ["b3"])
            pool._set_current("bg", "b1")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b3")
            pool._rotate_group_once("bg")
            self.assertEqual(pool._get_current("bg"), "b3")

    def test_rotate_members_normalization(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            self.assertEqual(pool._valid_group_rotate_members(None), [])
            self.assertEqual(pool._valid_group_rotate_members(""), [])
            self.assertEqual(pool._valid_group_rotate_members([]), [])
            self.assertEqual(pool._valid_group_rotate_members(["a", "a", " b "]), ["a", "b"])
            self.assertIsNone(pool._valid_group_rotate_members("a"))
            self.assertIsNone(pool._valid_group_rotate_members([1, 2]))

    def test_create_update_store_and_clear_rotate_members(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            pool = m.APIPool([])
            ok, msg = pool.create_group("bg", "mixed", "", 0, 0, "all", 5, ["e1", "e2"])
            self.assertTrue(ok, msg)
            self.assertEqual(pool._group_rotate_members("bg"), ["e1", "e2"])
            # 非法拒绝
            ok, msg = pool.create_group("bad", "mixed", "", 0, 0, "all", 5, "e1")
            self.assertFalse(ok)
            self.assertIn("轮换成员", msg)
            # 编辑改集 / 清空（空 = 全体参与，不落键）
            ok, _ = pool.update_group("bg", {"rotate_members": ["e2"]})
            self.assertTrue(ok)
            self.assertEqual(pool._group_rotate_members("bg"), ["e2"])
            ok, _ = pool.update_group("bg", {"rotate_members": []})
            self.assertTrue(ok)
            self.assertNotIn("rotate_members", pool._group_defs["bg"])
            # 默认态不落键
            pool.create_group("bg2", "mixed", "", 0, 0, "all", 5)
            self.assertNotIn("rotate_members", pool._group_defs["bg2"])

    def test_rotate_members_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            m = load_module(tmp)
            m.pool.create_group("bg", "mixed", "", 0, 0, "all", 15, ["e1", "e3"])
            m._sync_to_config()
            saved = json.load(open(m.CONFIG_FILE, encoding="utf-8"))
            entry = next(d for d in saved["pool_group_defs"] if d["name"] == "bg")
            self.assertEqual(entry["rotate_members"], ["e1", "e3"])
            raw = m.load_group_defs_config()
            pool2 = m.APIPool([])
            pool2._load_group_defs(raw)
            self.assertEqual(pool2._group_rotate_members("bg"), ["e1", "e3"])
            self.assertEqual(pool2._group_rotate_members("main"), [])   # 默认态读回空


if __name__ == "__main__":
    unittest.main()
