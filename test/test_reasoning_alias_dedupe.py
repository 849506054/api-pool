"""reasoning 双载体去重自检（2026-11-02）：reasoning_content 与 reasoning 同文 → 只留前者。

背景：Hermes 对 custom provider 走本机载体 {reasoning_content, reasoning}，同一段思考两个键
都回放；上游把 reasoning 当 reasoning_content 的别名（serde alias）时 400
`duplicate field reasoning_content`（Tokenrhythm18 实测，req=303a973d）。可直接 python3 运行。
"""
import importlib.util

spec = importlib.util.spec_from_file_location("aps", "/opt/data/work/api-pool2/api_pool_server.py")
aps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aps)

APIPool = aps.APIPool
Endpoint = aps.Endpoint


def ep(model, **kw):
    kw.setdefault("base_url", "https://x.example/v1")
    return Endpoint(name="t", api_key="k", model=model, **kw)


# 1) 双载体同文 → 只留 reasoning_content，原始历史不变
msgs = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "ok", "reasoning_content": "R1", "reasoning": "R1"},
]
out = APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))
assert out[1] == {"role": "assistant", "content": "ok", "reasoning_content": "R1"}, out[1]
assert msgs[1]["reasoning"] == "R1", "原始历史不得被改写"

# 2) 两键文本不同 → 都保留（不猜哪一份对）
msgs = [{"role": "assistant", "content": "ok", "reasoning_content": "R1", "reasoning": "R2"}]
out = APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))
assert out[0].get("reasoning_content") == "R1" and out[0].get("reasoning") == "R2", out[0]

# 3) 只有 reasoning（无 reasoning_content）→ 原样保留
msgs = [{"role": "assistant", "content": "ok", "reasoning": "R1"}]
out = APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))
assert out[0].get("reasoning") == "R1", out[0]

# 4) 双载体空串 + tool_calls → cmp 删别名，reasoning_content 补占位
msgs = [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}],
         "reasoning_content": "", "reasoning": ""}]
out = APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))
assert out[0].get("reasoning_content") == " " and "reasoning" not in out[0], out[0]

# 5) 单载体消息零拷贝（对象同一）
msgs = [{"role": "assistant", "content": "ok", "reasoning_content": "R1"}]
out = APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))
assert out[0] is msgs[0], "无改动时应返回原对象"

# 6) 剥离家族（非 reasoning 端点）：reasoning 键保持既有行为，不被本次去重误伤
msgs = [{"role": "assistant", "content": "ok", "reasoning_content": "R1", "reasoning": "R1"}]
out = APIPool._messages_for_endpoint(msgs, ep("gpt-5.6-sol"))
assert "reasoning_content" not in out[0] and out[0].get("reasoning") == "R1", out[0]

# 7) 序列化后单条消息内不出现重复键（端到端护栏）
import json
msgs = [{"role": "assistant", "content": "ok", "reasoning_content": "R1", "reasoning": "R1"}]
wire = APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))
text = json.dumps({"messages": wire}, ensure_ascii=False)
pairs = []
json.loads(text, object_pairs_hook=lambda kv: pairs.extend([k for k, _ in kv]) or dict(kv))
assert pairs.count("reasoning_content") == 1 and "reasoning" not in pairs, pairs

print("ALL ASSERTS PASSED")
