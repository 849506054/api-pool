"""api-pool-switch 分组适配 — 纯函数测试 + live API 契约验证。

运行: python3 tests/test_group_adapt.py [--live]
  --live: 直接打生产 API Pool (192.168.5.6:5200) 核对字段语义
"""

import importlib.util
import os
import sys
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(PLUGIN_DIR, "__init__.py")

spec = importlib.util.spec_from_file_location("api_pool_switch_test", MODULE_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

LIVE = "--live" in sys.argv


def live_endpoints():
    return mod._api_get("/api/endpoints")


def live_groups():
    d = mod._api_get("/api/groups")
    return d.get("groups", []) if isinstance(d, dict) else (d or [])


# 与生产结构一致的合成样本（字段名对齐 _ep_to_dict）
SAMPLE_EPS = [
    {"id": "e1", "name": "Alpha", "in_pool": True, "pool_groups": ["main", "pool-bg"],
     "priority": 5, "priority_by_group": {"main": 3, "pool-bg": 1},
     "current_groups": ["main"], "enabled": True, "in_cooldown": False,
     "health": "ok", "model": "m1"},
    {"id": "e2", "name": "Beta", "in_pool": True, "pool_groups": ["main"],
     "priority": 2, "priority_by_group": {"main": 2},
     "current_groups": [], "enabled": True, "in_cooldown": False,
     "health": "unknown", "model": "m2"},
    {"id": "e3", "name": "Gamma", "in_pool": True, "pool_groups": ["main", "pool-bg"],
     "priority": 1, "priority_by_group": {"main": 1, "pool-bg": 2},
     "current_groups": ["pool-bg"], "enabled": False, "in_cooldown": False,
     "health": "bad", "model": "m3"},
    {"id": "e4", "name": "Delta", "in_pool": False, "pool_groups": [],
     "priority": 1, "priority_by_group": {}, "current_groups": [],
     "enabled": True, "in_cooldown": False, "health": "unknown", "model": "m4"},
]

SAMPLE_GROUPS = [
    {"name": "pool-bg", "type": "mixed", "model": "api-pool-bg", "members": 2},
    {"name": "main", "type": "mixed", "model": "api-pool", "members": 3},
]


class GroupShapingTests(unittest.TestCase):
    def test_group_priority_uses_pbg_then_priority(self):
        self.assertEqual(mod._ep_group_priority(SAMPLE_EPS[0], "pool-bg"), 1)
        self.assertEqual(mod._ep_group_priority(SAMPLE_EPS[0], "main"), 3)
        # 无 pbg 记录 → 回退全局 priority
        self.assertEqual(mod._ep_group_priority(SAMPLE_EPS[1], "main"), 2)

    def test_group_members_filters_and_sorts_by_group_priority(self):
        members = mod._group_members(SAMPLE_EPS, "main")
        self.assertEqual([e["name"] for e in members], ["Gamma", "Beta", "Alpha"])
        # pool-bg 内 Alpha 的 pbg=1 排 Gamma pbg=2 前
        bg = mod._group_members(SAMPLE_EPS, "pool-bg")
        self.assertEqual([e["name"] for e in bg], ["Alpha", "Gamma"])

    def test_non_pool_endpoint_excluded(self):
        members = mod._group_members(SAMPLE_EPS, "main")
        self.assertNotIn("Delta", [e["name"] for e in members])

    def test_all_group_names_api_order_with_main_first(self):
        names = mod._all_group_names(SAMPLE_EPS, SAMPLE_GROUPS)
        self.assertEqual(names[0], "main")
        self.assertEqual(set(names), {"main", "pool-bg"})

    def test_current_per_group(self):
        self.assertTrue(mod._group_is_current(SAMPLE_EPS[0], "main"))
        self.assertFalse(mod._group_is_current(SAMPLE_EPS[0], "pool-bg"))
        self.assertTrue(mod._group_is_current(SAMPLE_EPS[2], "pool-bg"))


    def test_group_menu_text_is_single_line_per_group(self):
        text = mod._group_menu_text(SAMPLE_EPS, ["main", "pool-bg"])
        lines = text.splitlines()
        self.assertEqual(lines[1:], ["main · 当前 Alpha · 3 端点", "pool-bg · 当前 Gamma · 2 端点"])

    def test_endpoint_menu_text_is_compact(self):
        text = mod._endpoint_menu_text("pool-bg", SAMPLE_EPS)
        self.assertEqual(text.splitlines(), [
            "**端点切换 · pool-bg**",
            "当前: **Gamma** — m3",
            "2 个端点（按组内优先级）",
        ])


class ChoiceBuildTests(unittest.TestCase):
    def test_group_choices_label_has_current_endpoint(self):
        choices = mod._group_choices(SAMPLE_EPS, ["main", "pool-bg"])
        main = next(c for c in choices if c["callback_value"] == "group:main")
        self.assertIn("Alpha", main["label"])  # main 当前端点
        self.assertTrue(main["full_width"])

    def test_endpoint_choices_sorted_without_priority_prefix(self):
        choices = mod._endpoint_choices("main", SAMPLE_EPS)
        # 仍按优先级排序，但按钮标签不显示优先级编号。
        self.assertEqual([c["callback_value"] for c in choices],
                         ["ep:main:e3", "ep:main:e2", "ep:main:e1"])
        gamma = next(c for c in choices if c["callback_value"] == "ep:main:e3")
        self.assertNotIn("#1", gamma["label"])
        self.assertIn("🔴", gamma["label"])
        self.assertTrue(gamma["full_width"])
        alpha = next(c for c in choices if c["callback_value"] == "ep:main:e1")
        self.assertTrue(alpha["is_current"])  # main 当前

    def test_health_badge(self):
        self.assertEqual(mod._health_badge(SAMPLE_EPS[2]), " 🔴")  # disabled
        self.assertEqual(mod._health_badge(SAMPLE_EPS[0]), "")  # ok


class LiveContractTests(unittest.TestCase):
    """打生产 API 核对字段语义（--live 才跑）。"""

    def setUp(self):
        if not LIVE:
            self.skipTest("skip live (add --live)")

    def test_live_fields_present(self):
        eps = live_endpoints()
        for e in eps:
            self.assertIn("pool_groups", e)
            self.assertIn("priority_by_group", e)
            self.assertIn("current_groups", e)
            self.assertIn("in_pool", e)

    def test_live_groups_and_endpoints_consistent(self):
        eps = live_endpoints()
        groups = live_groups()
        names = mod._all_group_names(eps, groups)
        self.assertTrue(names)
        self.assertEqual(names[0], "main")
        # 每个组至少声明 1 个成员
        for g in groups:
            self.assertGreaterEqual(g["members"], 1)
        # 无端点不在任何组却在池内（组定义与端点声明一致）
        for e in eps:
            if e.get("in_pool"):
                self.assertTrue(e.get("pool_groups"), f"{e['name']} in_pool but no group")

    def test_live_group_members_sorted_and_current_marked(self):
        eps = live_endpoints()
        for gname in mod._all_group_names(eps, live_groups()):
            members = mod._group_members(eps, gname)
            prios = [mod._ep_group_priority(e, gname) for e in members]
            self.assertEqual(prios, sorted(prios), f"group {gname} not sorted by priority")
            # 组当前端点在 current_groups 中
            for e in eps:
                if gname in (e.get("current_groups") or []):
                    self.assertIn(e["id"], [m["id"] for m in members],
                                  f"{e['name']} current for {gname} but not member?")


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:] if a != "--live"])
