"""api-pool-switch 分组适配 — 回调值解析与菜单构建逻辑。

运行: python3 tests/test_callback_coding.py
"""

import importlib.util
import os
import sys
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("api_pool_switch_cb_test", os.path.join(PLUGIN_DIR, "__init__.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 合成端点（对齐 /api/endpoints 字段）
EPS = [
    {"id": "aaa", "name": "Alpha", "in_pool": True, "pool_groups": ["main"],
     "priority": 3, "priority_by_group": {"main": 3}, "current_groups": ["main"],
     "enabled": True, "in_cooldown": False, "health": "ok", "model": "m1"},
    {"id": "bbb", "name": "Beta", "in_pool": True, "pool_groups": ["main", "pool-bg"],
     "priority": 2, "priority_by_group": {"main": 2, "pool-bg": 1},
     "current_groups": [], "enabled": True, "in_cooldown": True, "health": "ok",
     "model": "m2"},
    {"id": "ccc", "name": "Gamma", "in_pool": True, "pool_groups": ["pool-bg"],
     "priority": 1, "priority_by_group": {"pool-bg": 2}, "current_groups": ["pool-bg"],
     "enabled": True, "in_cooldown": False, "health": "bad", "model": "m3"},
    {"id": "ddd", "name": "Delta", "in_pool": False, "pool_groups": [],
     "priority": 9, "priority_by_group": {}, "current_groups": [],
     "enabled": True, "in_cooldown": False, "health": "unknown", "model": "m4"},
]


class CallbackCodingTests(unittest.TestCase):
    def test_group_value_encoding(self):
        for choice in mod._group_choices(EPS, ["main", "pool-bg"]):
            self.assertTrue(choice["callback_value"].startswith("group:"))
            self.assertIn(choice["callback_value"].split(":", 1)[1], ("main", "pool-bg"))

    def test_endpoint_value_encoding(self):
        for choice in mod._endpoint_choices("pool-bg", EPS):
            v = choice["callback_value"]
            self.assertTrue(v.startswith("ep:pool-bg:"), v)
            ep_id = v.split(":", 2)[2]
            self.assertIn(ep_id, ("aaa", "bbb", "ccc"))

    def test_group_label_contains_current(self):
        labels = {c["callback_value"]: c["label"] for c in mod._group_choices(EPS, ["main", "pool-bg"])}
        self.assertIn("Alpha", labels["group:main"])       # main 当前 Alpha
        self.assertIn("Gamma", labels["group:pool-bg"])    # pool-bg 当前 Gamma

    def test_cooldown_marks_frozen_not_current(self):
        choices = mod._endpoint_choices("main", EPS)
        beta = next(c for c in choices if c["callback_value"] == "ep:main:bbb")
        self.assertIn("🔴", beta["label"])       # cooldown → 🔴
        alpha = next(c for c in choices if c["callback_value"] == "ep:main:aaa")
        self.assertTrue(alpha["is_current"])     # Alpha 是 main 当前
        self.assertNotIn("🔴", alpha["label"])

    def test_picker_endpoint_switch_posts_structured_group_request(self):
        import asyncio
        calls = []
        original_post = mod._api_post
        try:
            mod._api_post = lambda path, body=None: calls.append((path, body)) or {
                "ok": True,
                "group": "pool-bg",
                "endpoint_name": "Gamma",
                "model": "m3",
                "current": True,
            }
            mod._PICKER_CTX["chatY"] = {"adapter": None, "source": None, "metadata": None}
            result = asyncio.run(mod._on_choice_selected("chatY", "ep:pool-bg:ccc"))
        finally:
            mod._api_post = original_post
        self.assertIn("Gamma", result)
        self.assertEqual(calls, [(
            "/api/pool/switch",
            {"group": "pool-bg", "endpoint_id": "ccc"},
        )])

    def test_second_level_waits_for_picker_state_release(self):
        import asyncio
        calls = []
        original_open = mod._open_endpoint_menu
        try:
            async def fake_open(context, group):
                calls.append(group)

            mod._open_endpoint_menu = fake_open
            state = {"chatZ": {"old": True}}
            adapter = type("Adapter", (), {"_choice_picker_state": state})()
            context = {"adapter": adapter}
            mod._PICKER_CTX["chatZ"] = context

            async def run():
                result = await mod._on_choice_selected("chatZ", "group:pool-bg")
                self.assertIn("pool-bg", result)
                await asyncio.sleep(0.05)
                self.assertEqual(calls, [])
                state.pop("chatZ", None)
                await asyncio.sleep(0.1)

            asyncio.run(run())
        finally:
            mod._open_endpoint_menu = original_open
            mod._PICKER_CTX.pop("chatZ", None)
        self.assertEqual(calls, ["pool-bg"])

    def test_picker_value_routing(self):
        """值路由：group: 前缀开二级；ep: 前缀切换；取消清理上下文。"""
        # 用假上下文验证 forget 语义
        mod._PICKER_CTX["chatX"] = {"adapter": None, "source": None, "metadata": None}

        # 取消
        import asyncio
        r1 = asyncio.run(mod._on_choice_selected("chatX", "cancel"))
        self.assertIn("取消", r1)
        self.assertNotIn("chatX", mod._PICKER_CTX)

        # 无效值（上下文已清）→ 提示失效
        r2 = asyncio.run(mod._on_choice_selected("chatX", "group:main"))
        self.assertIn("失效", r2)


if __name__ == "__main__":
    unittest.main()
