"""GLM 适配自检：reasoning_policy / reasoning_effort 映射 / preserved_thinking。可直接 python3 运行。"""
import importlib.util
import sys

spec = importlib.util.spec_from_file_location("aps", "/opt/data/work/api-pool2/api_pool_server.py")
aps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(aps)  # 顶层仅定义，main() 不触发

APIPool = aps.APIPool
Endpoint = aps.Endpoint


def ep(model, **kw):
    kw.setdefault("base_url", "https://x.example/v1")
    return Endpoint(name="t", api_key="k", model=model, **kw)


msgs = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "ok", "reasoning_content": "R1"},
    {"role": "assistant", "content": "ok2", "reasoning_text": "R2"},
]

# 1) auto 策略：glm/deepseek 保留，其他剥离
assert APIPool._messages_for_endpoint(msgs, ep("glm-5.3"))[1].get("reasoning_content") == "R1"
assert APIPool._messages_for_endpoint(msgs, ep("deepseek-v4-flash"))[1].get("reasoning_content") == "R1"
assert "reasoning_content" not in APIPool._messages_for_endpoint(msgs, ep("gpt-5.6-sol"))[1]
assert "reasoning_text" not in APIPool._messages_for_endpoint(msgs, ep("gpt-5.6-sol"))[2]

# 2) keep/strip 覆盖启发式
assert APIPool._messages_for_endpoint(msgs, ep("auto", reasoning_policy="keep"))[1].get("reasoning_content") == "R1"
assert "reasoning_content" not in APIPool._messages_for_endpoint(msgs, ep("deepseek-v3", reasoning_policy="strip"))[1]

# 2b) auto 家族对齐 Hermes：kimi/moonshot/mimo 保留，其余剥离
assert APIPool._messages_for_endpoint(msgs, ep("kimi-k3"))[1].get("reasoning_content") == "R1"
assert APIPool._messages_for_endpoint(msgs, ep("k", base_url="https://api.moonshot.cn/v1"))[1].get("reasoning_content") == "R1"
assert APIPool._messages_for_endpoint(msgs, ep("mimo-7b"))[1].get("reasoning_content") == "R1"
assert "reasoning_content" not in APIPool._messages_for_endpoint(msgs, ep("gpt-5.6-sol"))[1]

# 3) reasoning_effort 映射：GLM/Kimi/DeepSeek；MiMo 官方未定义该参数，保持透传
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("glm-5.3"))
assert p["reasoning_effort"] == "high", p
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("glm-5.3-flash"))
assert p["reasoning_effort"] == "high"
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("glm-5.2"))  # 服务端自映射，透传
assert p["reasoning_effort"] == "medium"
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("glm-4.6"))
assert "reasoning_effort" not in p
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("deepseek-v4-flash"))
assert p["reasoning_effort"] == "medium"
p = {"reasoning_effort": "xhigh"}
APIPool._map_reasoning_effort(p, ep("deepseek-v4-flash"))
assert p["reasoning_effort"] == "max"
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("kimi-k3"))
assert p["reasoning_effort"] == "high"
p = {"reasoning_effort": "xhigh"}
APIPool._map_reasoning_effort(p, ep("kimi-k3"))
assert p["reasoning_effort"] == "max"
p = {"reasoning_effort": "max"}
APIPool._map_reasoning_effort(p, ep("kimi-k2.6"))
assert p["reasoning_effort"] == "high"
p = {"reasoning_effort": "medium"}
APIPool._map_reasoning_effort(p, ep("mimo-v2.5-pro"))
assert p["reasoning_effort"] == "medium"  # 官方未定义参数词汇，不猜映射
p = {}  # 未显式设置：不动
APIPool._map_reasoning_effort(p, ep("glm-5.3"))
assert p == {}

# 4) preserved_thinking 注入语义（模拟轮转处 payload 构造）
e = ep("glm-5.3", preserved_thinking=True)
payload = {"model": "glm-5.3", "messages": []}
APIPool._map_reasoning_effort(payload, e)
t = payload.get("thinking")
if isinstance(t, dict):
    t["clear_thinking"] = False
else:
    payload["thinking"] = {"type": "enabled", "clear_thinking": False}
assert payload["thinking"] == {"type": "enabled", "clear_thinking": False}

# 5) 序列化往返：新字段进 _ep_to_dict
pool = APIPool()
pool.add_endpoint({"name": "g", "base_url": "https://x/v1", "api_key": "k", "model": "glm-5.3",
                   "reasoning_policy": "keep", "preserved_thinking": True})
d = pool.list_endpoints()[0]
assert d["reasoning_policy"] == "keep" and d["preserved_thinking"] is True

print("ALL ASSERTS PASSED")
