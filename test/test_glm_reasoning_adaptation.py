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

# 6) 关闭思考的 GLM 折叠（2026-09-11）：disabled/none → enabled + effort low
p = {"thinking": {"type": "disabled"}}
APIPool._normalize_glm_thinking(p, ep("glm-5.3"))
assert p["thinking"] == {"type": "enabled"} and p["reasoning_effort"] == "low", p
p = {"thinking": {"type": "none"}}
APIPool._normalize_glm_thinking(p, ep("glm-5.3-flash"))
assert p["thinking"] == {"type": "enabled"} and p["reasoning_effort"] == "low", p
# 客户端已显式给档位：只折叠 thinking，不覆盖更高档
p = {"thinking": {"type": "disabled"}, "reasoning_effort": "high"}
APIPool._normalize_glm_thinking(p, ep("glm-5.3"))
assert p["reasoning_effort"] == "high" and p["thinking"] == {"type": "enabled"}, p
# preserved_thinking 的注入体（enabled + clear_thinking）不动
p = {"thinking": {"type": "enabled", "clear_thinking": False}}
APIPool._normalize_glm_thinking(p, ep("glm-5.3"))
assert p["thinking"] == {"type": "enabled", "clear_thinking": False}, p
# glm-5.2 支持关闭思考；非 GLM 端点：不干预
p = {"thinking": {"type": "disabled"}}
APIPool._normalize_glm_thinking(p, ep("glm-5.2"))
assert p["thinking"] == {"type": "disabled"} and "reasoning_effort" not in p, p

# 7) 回归护栏：不再有「关闭 thinking」注入（结构性无效 + 会污染轮转后的异构端点）。
#    严格校验 400 重试为**二级口径**（2026-09-12，外部实证 zdsub2api 后定案）：端点级上限
#    strict400_retries（0/1/2，默认 2）；第 1 级同端点原样重试 + 抖动退避，第 2 级
#    「原始 tool_call id」变体仅在本次确实应用过前缀重写时发；且只在**首包前**失败分支上重试。
src = open("/opt/data/work/api-pool2/api_pool_server.py", encoding="utf-8").read()
assert "disable_thinking_forced" not in src
assert "disable_thinking_eps" not in src
assert 'payload["thinking"] = {"type": "disabled"}' not in src
assert "strict_validation_retries = 0" in src
assert "strict_validation_retries < 1 and budget >= 1" in src, "第 1 级：同端点原样重试"
assert "strict_validation_retries = 1" in src
assert "budget >= 2 and not strict_variant_used and attempt_prefix_applied > 0" in src, "第 2 级：变体受前缀约束"
assert "skip_prefix_rewrite = True" in src
assert "strict400_retries: int = 2" in src, "端点级上限默认 2"
assert "_STRICT400_BACKOFF_MS" in src, "重试前抖动退避"
assert "第 1 级 原样重试" in src and "第 2 级 改用客户端原始 tool_call id" in src
assert src.count("self._is_strict_validation_400(error)") == 1, "重试只在首包前的失败分支上发生"
assert "loop_messages, attempt_prefix_applied = self._rewrite_tool_call_ids(" in src

print("ALL ASSERTS PASSED")
