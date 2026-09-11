"""回归：chat/completions → Responses 上游桥的请求体形状。

2026-09-12 修复两处（ps.air-outer AgentRouter /v1/responses 严格校验）：
1. assistant 消息正文必须是 output_text（input_text 直接 400）；
2. 顶层 reasoning_effort 必须转成 reasoning.effort 透传，否则客户端设置静默丢失。
"""

import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_original_thread = threading.Thread


class _NoStartThread(_original_thread):
    def start(self):
        return None


threading.Thread = _NoStartThread
import api_pool_server as m

threading.Thread = _original_thread


def _items(messages):
    return m._chat_messages_to_responses_input(messages)[0]


def test_assistant_content_is_output_text():
    items = _items([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "好的，我来调用。",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "get_time", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "12:00"},
        {"role": "user", "content": "现在几点"},
    ])
    roles = [(i["role"], [p["type"] for p in i["content"]]) for i in items if i["type"] == "message"]
    assert roles == [("user", ["input_text"]), ("assistant", ["output_text"]), ("user", ["input_text"])], roles
    # tool_calls 仍按 function_call 项输出，未被影响
    assert [i["type"] for i in items].count("function_call") == 1


def test_reasoning_effort_passthrough():
    body = m._responses_body_from_chat({"messages": [{"role": "user", "content": "hi"}],
                                        "reasoning_effort": "medium"})
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}, body.get("reasoning")
    # Hermes 可发 minimal，上游只收 none/low/medium/high/xhigh/max → 钳到 low
    body = m._responses_body_from_chat({"messages": [{"role": "user", "content": "hi"}],
                                        "reasoning_effort": "MINIMAL"})
    assert body["reasoning"] == {"effort": "low", "summary": "auto"}, body.get("reasoning")
    # 没有显式值时不下发，交上游默认
    body = m._responses_body_from_chat({"messages": [{"role": "user", "content": "hi"}]})
    assert "reasoning" not in body, body.get("reasoning")
    # 已经是 Responses 形态的 reasoning 优先，不被顶层 effort 覆盖
    body = m._responses_body_from_chat({"messages": [{"role": "user", "content": "hi"}],
                                        "reasoning": {"effort": "high", "summary": None},
                                        "reasoning_effort": "low"})
    assert body["reasoning"]["effort"] == "high", body.get("reasoning")


if __name__ == "__main__":
    failed = 0
    for fn in (test_assistant_content_is_output_text, test_reasoning_effort_passthrough):
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc!r}")
    sys.exit(1 if failed else 0)
