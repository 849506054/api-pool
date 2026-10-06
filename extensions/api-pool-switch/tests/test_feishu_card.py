"""api-pool-switch 飞书卡片 — 卡片结构 + 按钮回调路由。

运行: python3 tests/test_feishu_card.py
"""

import asyncio
import importlib.util
import json
import os
import unittest
from types import SimpleNamespace

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("api_pool_switch_feishu_test", os.path.join(PLUGIN_DIR, "__init__.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 合成端点（对齐 /api/endpoints 字段）
EPS = [
    {"id": "aaa", "name": "Alpha", "in_pool": True, "pool_groups": ["main"],
     "priority": 3, "priority_by_group": {"main": 3}, "current_groups": ["main"],
     "enabled": True, "in_cooldown": False, "health": "ok", "model": "m1"},
    {"id": "bbb", "name": "Beta", "in_pool": True, "pool_groups": ["main", "pool-bg"],
     "priority": 2, "priority_by_group": {"main": 2, "pool-bg": 1},
     "current_groups": [], "enabled": True, "in_cooldown": True, "health": "ok", "model": "m2"},
    {"id": "ccc", "name": "Gamma", "in_pool": True, "pool_groups": ["pool-bg"],
     "priority": 1, "priority_by_group": {"pool-bg": 2}, "current_groups": ["pool-bg"],
     "enabled": True, "in_cooldown": False, "health": "bad", "model": "m3"},
]

GROUPS = {"groups": [{"name": "main", "members": 2}, {"name": "pool-bg", "members": 2}]}

SOURCE = SimpleNamespace(platform=SimpleNamespace(value="feishu"), chat_id="oc_test", user_id="ou_test")


class FakeEvent:
    def __init__(self, text, raw_message=None):
        self.text = text
        self.source = SOURCE
        self.message_id = "om_in"
        self.raw_message = raw_message

    def get_command(self):
        if not self.text.startswith("/"):
            return None
        return self.text[1:].split(maxsplit=1)[0].lower()

    def get_command_args(self):
        parts = self.text.split(maxsplit=1)
        return parts[1] if len(parts) > 1 else ""


class FakeAdapter:
    def __init__(self, operator_authorized=True):
        self.cards = []
        self.texts = []
        self.operator_authorized = operator_authorized
        self.operators_seen = []

    async def _feishu_send_with_retry(self, *, chat_id, msg_type, payload, reply_to, metadata):
        self.cards.append((chat_id, msg_type, json.loads(payload)))
        return {"code": 0}

    def _finalize_send_result(self, response, default_message):
        return SimpleNamespace(success=True, error=None, message_id="om_card")

    async def send(self, chat_id, content, **kwargs):
        self.texts.append((chat_id, content))

    def _is_interactive_operator_authorized(self, open_id):
        self.operators_seen.append(open_id)
        return self.operator_authorized


class FakeGateway:
    def __init__(self, adapter):
        self.adapter = adapter

    def _is_user_authorized(self, source):
        return True

    def _adapter_for_source(self, source):
        return self.adapter

    def _session_key_for_source(self, source):
        return "sess"

    def _thread_metadata_for_source(self, source, message_id):
        return None


def buttons(card):
    return [e["actions"][0] for e in card["elements"] if e["tag"] == "action"]


class CardShapeTests(unittest.TestCase):
    def test_group_card_is_header_plus_one_button_per_group(self):
        card = mod._feishu_group_card(EPS, ["main", "pool-bg"])
        self.assertEqual(card["header"]["title"]["content"], "🗂 端点切换 · 选择池组")
        self.assertEqual(card["elements"][0]["tag"], "markdown")
        self.assertIn("**main** · 当前 Alpha", card["elements"][0]["content"])
        self.assertEqual([b["value"]["apipool"] for b in buttons(card)],
                         ["group:main", "group:pool-bg"])

    def test_endpoint_card_marks_current_and_frozen(self):
        card = mod._feishu_endpoint_card("main", EPS)
        self.assertEqual(card["header"]["title"]["content"], "端点切换 · main")
        got = {b["value"]["apipool"]: b for b in buttons(card)}
        self.assertEqual(sorted(got), ["ep:main:aaa", "ep:main:bbb"])
        current = got["ep:main:aaa"]
        self.assertEqual(current["type"], "primary")
        self.assertEqual(current["text"]["content"], "✓ Alpha")
        frozen = got["ep:main:bbb"]
        self.assertEqual(frozen["type"], "default")
        self.assertIn("🔴", frozen["text"]["content"])

    def test_button_payload_is_plain_text_and_keyed(self):
        card = mod._feishu_group_card(EPS, ["main"])
        button = buttons(card)[0]
        self.assertEqual(button["text"]["tag"], "plain_text")
        self.assertEqual(button["tag"], "button")


class ResultCardTests(unittest.TestCase):
    def test_title_follows_subcommand(self):
        self.assertEqual(mod._feishu_result_title("switch Alpha"), "🔀 端点切换")
        self.assertEqual(mod._feishu_result_title("check"), "🩺 健康检查结果")
        self.assertEqual(mod._feishu_result_title("health"), "🩺 端点健康详情")
        self.assertEqual(mod._feishu_result_title("bogus"), "端点命令")

    def test_heading_line_is_dropped_and_body_kept(self):
        card = mod._feishu_result_card("端点命令", "**Health Check Results**\n- 🟢 **Alpha**  `ok`  12ms")
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["elements"][0]["content"], "- 🟢 **Alpha**  `ok`  12ms")

    def test_template_follows_outcome(self):
        cases = {"✅ 已切换": "green", "❌ 切换失败": "red", "⚠️ 无端点": "orange", "❓ Usage": "orange"}
        for text, template in cases.items():
            self.assertEqual(mod._feishu_result_card("x", text)["header"]["template"], template, text)


class ActionValueTests(unittest.TestCase):
    def test_parses_adapter_synthetic_command(self):
        # 适配器 json.dumps 默认分隔符（": " / ", "）
        raw = '/card button {"apipool": "ep:main:aaa"}'
        self.assertEqual(mod._feishu_action_value(raw.split(maxsplit=1)[1]), "ep:main:aaa")

    def test_ignores_foreign_and_malformed_payloads(self):
        self.assertEqual(mod._feishu_action_value('button {"hermes_action": "allow"}'), "")
        self.assertEqual(mod._feishu_action_value("button not-json"), "")
        self.assertEqual(mod._feishu_action_value(""), "")


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.adapter = FakeAdapter()
        self.gateway = FakeGateway(self.adapter)
        self._orig_get = mod._api_get
        self._orig_post = mod._api_post
        mod._api_get = lambda path: EPS if "endpoints" in path else GROUPS

    def tearDown(self):
        mod._api_get = self._orig_get
        mod._api_post = self._orig_post

    def _run_event(self, event):
        async def run():
            result = mod._feishu_dispatch(event, self.gateway, SOURCE)
            await asyncio.sleep(0.1)
            return result

        return asyncio.run(run())

    def _run(self, text):
        return self._run_event(FakeEvent(text))

    def test_bare_endpoint_sends_group_card(self):
        result = self._run("/endpoint")
        self.assertEqual(result["action"], "skip")
        self.assertEqual(len(self.adapter.cards), 1)
        chat_id, msg_type, card = self.adapter.cards[0]
        self.assertEqual((chat_id, msg_type), ("oc_test", "interactive"))
        self.assertEqual(card["header"]["title"]["content"], "🗂 端点切换 · 选择池组")

    def test_endpoint_with_args_sends_result_card(self):
        original = mod.handle_endpoint

        async def fake_handle(args):
            self.assertEqual(args, "health")
            return "**Health Details (pool endpoints)**\n- **Alpha** 🟢"

        mod.handle_endpoint = fake_handle
        try:
            result = self._run("/endpoint health")
        finally:
            mod.handle_endpoint = original
        self.assertEqual(result["action"], "skip")
        self.assertEqual(len(self.adapter.cards), 1)
        card = self.adapter.cards[0][2]
        self.assertEqual(card["header"]["title"]["content"], "🩺 端点健康详情")
        self.assertEqual(card["elements"][0]["content"], "- **Alpha** 🟢")

    def test_group_click_opens_endpoint_card(self):
        result = self._run('/card button {"apipool": "group:pool-bg"}')
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.adapter.cards[0][2]["header"]["title"]["content"],
                         "端点切换 · pool-bg")

    def test_endpoint_click_switches_and_replies(self):
        calls = []
        mod._api_post = lambda path, body=None: calls.append((path, body)) or {
            "ok": True, "group": "main", "endpoint_name": "Alpha", "model": "m1",
        }
        result = self._run('/card button {"apipool": "ep:main:aaa"}')
        self.assertEqual(result["action"], "skip")
        self.assertEqual(calls, [("/api/pool/switch", {"group": "main", "endpoint_id": "aaa"})])
        self.assertEqual(len(self.adapter.texts), 1)
        self.assertIn("Alpha", self.adapter.texts[0][1])

    def test_foreign_card_action_is_not_claimed(self):
        self.assertIsNone(self._run('/card button {"hermes_clarify_action": true}'))
        self.assertIsNone(self._run('/card button not-json'))
        self.assertEqual(self.adapter.cards, [])
        self.assertEqual(self.adapter.texts, [])

    def test_unknown_group_reports_without_card(self):
        result = self._run('/card button {"apipool": "group:ghost"}')
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.adapter.cards, [])
        self.assertIn("ghost", self.adapter.texts[0][1])

    def test_click_uses_raw_callback_operator(self):
        raw = SimpleNamespace(event=SimpleNamespace(operator=SimpleNamespace(open_id="ou_from_callback")))
        result = self._run_event(FakeEvent('/card button {"apipool": "group:main"}', raw_message=raw))
        self.assertEqual(result["action"], "skip")
        self.assertEqual(self.adapter.operators_seen, ["ou_from_callback"])
        self.assertEqual(self.adapter.cards[0][2]["header"]["title"]["content"], "端点切换 · main")

    def test_click_operator_falls_back_to_source_user_id(self):
        self._run('/card button {"apipool": "group:main"}')
        self.assertEqual(self.adapter.operators_seen, ["ou_test"])

    def test_click_rejected_when_operator_unauthorized(self):
        self.adapter.operator_authorized = False
        result = self._run('/card button {"apipool": "group:main"}')
        self.assertIn("unauthorized", result["reason"])
        self.assertEqual(self.adapter.cards, [])
        self.assertEqual(self.adapter.texts, [])

    def test_click_denied_when_adapter_lacks_operator_gate(self):
        """老适配器没有 _is_interactive_operator_authorized → 拒绝（fail closed）。"""
        gateway = FakeGateway(SimpleNamespace())

        async def run():
            return mod._feishu_dispatch(FakeEvent('/card button {"apipool": "group:main"}'), gateway, SOURCE)

        self.assertIn("unauthorized", asyncio.run(run())["reason"])


if __name__ == "__main__":
    unittest.main()
