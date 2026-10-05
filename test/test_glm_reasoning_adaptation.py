"""GLM 适配自检：reasoning_policy / 端点级思考映射（reasoning_effort_map）/ preserved_thinking。可直接 python3 运行。"""
import importlib.util

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

# 3) 端点级思考映射（2026-10-05）：配置驱动；不配 = 完全不干预
GLM_CFG = {"map": {"none": "low", "minimal": "low", "low": "low", "medium": "high",
                   "high": "high", "xhigh": "max", "max": "max", "*": "high"}, "disable": "low"}
DS_CFG = {"map": {"none": "low", "minimal": "low", "low": "low", "medium": "medium",
                  "high": "high", "xhigh": "max", "max": "max", "*": "high"}, "disable": "passthrough"}


def cfg_ep(model, cfg, **kw):
    return ep(model, reasoning_effort_map=cfg, **kw)


p = {"reasoning_effort": "medium"}
assert APIPool._apply_reasoning_effort_map(p, ep("glm-5.3")) is False, "不配则不接管"
assert p == {"reasoning_effort": "medium"}

p = {"reasoning_effort": "medium"}
assert APIPool._apply_reasoning_effort_map(p, cfg_ep("glm-5.3", GLM_CFG)) is True
assert p["reasoning_effort"] == "high", p          # 客户端 medium → 端点 high
p = {"reasoning_effort": "weird"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("glm-5.3", GLM_CFG))
assert p["reasoning_effort"] == "high", p          # `*` 兜底
p = {"reasoning_effort": "xhigh"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("deepseek-v4-flash", DS_CFG))
assert p["reasoning_effort"] == "max", p
p = {"reasoning_effort": "medium"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("x", {"map": {}}))
assert p["reasoning_effort"] == "medium", "无 map 无 `*` → 未命中不猜（透传）"

# 4) 「关闭思考」的落地形态
# disable=low：端点关不掉（GLM）→ thinking 折叠成 enabled，档位落 low
p = {"thinking": {"type": "disabled"}}
APIPool._apply_reasoning_effort_map(p, cfg_ep("glm-5.3", GLM_CFG))
assert p == {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}, p
# 客户端同时给了有效档位 → 保留并按 map 归一（medium → high），不降级
p = {"thinking": {"type": "disabled"}, "reasoning_effort": "medium"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("glm-5.3", GLM_CFG))
assert p == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}, p
# reasoning_effort=none（无 thinking）→ 按 map 归一，不注入 thinking
p = {"reasoning_effort": "none"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("glm-5.3", GLM_CFG))
assert p == {"reasoning_effort": "low"}, p
# disable=passthrough（DeepSeek 真关闭）：thinking 原样，档位仍归一
p = {"thinking": {"type": "disabled"}}
APIPool._apply_reasoning_effort_map(p, cfg_ep("deepseek-v4-flash", DS_CFG))
assert p == {"thinking": {"type": "disabled"}}, p
p = {"thinking": {"type": "disabled"}, "reasoning_effort": "xhigh"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("deepseek-v4-flash", DS_CFG))
assert p == {"thinking": {"type": "disabled"}, "reasoning_effort": "max"}, p
# map 目标写成 disabled = 「该档位改为关闭」，由 disable 决定写法
p = {"reasoning_effort": "medium"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("deepseek-v4-flash", {"map": {"medium": "disabled", "*": "high"}, "disable": "thinking"}))
assert p == {"thinking": {"type": "disabled"}}, p
# disable=strip：上游对该字段反向生效（如 agnes）→ 剥掉
p = {"thinking": {"type": "disabled"}, "reasoning_effort": "high"}
APIPool._apply_reasoning_effort_map(p, cfg_ep("agnes-3.0-flash", {"map": {}, "disable": "strip"}))
assert p == {"reasoning_effort": "high"}, p
# disable=none：用 none 档表达关闭
p = {"thinking": {"type": "disabled"}}
APIPool._apply_reasoning_effort_map(p, cfg_ep("x", {"map": {}, "disable": "none"}))
assert p == {"reasoning_effort": "none"}, p

# 5) preserved_thinking 注入语义（模拟轮转处 payload 构造）
e = cfg_ep("glm-5.3", GLM_CFG, preserved_thinking=True)
payload = {"model": "glm-5.3", "messages": []}
APIPool._apply_reasoning_effort_map(payload, e)
t = payload.get("thinking")
if isinstance(t, dict):
    t["clear_thinking"] = False
else:
    payload["thinking"] = {"type": "enabled", "clear_thinking": False}
assert payload["thinking"] == {"type": "enabled", "clear_thinking": False}

# 6) 序列化往返：新字段进 _ep_to_dict / list_endpoints
pool = APIPool()
pool.add_endpoint({"name": "g", "base_url": "https://x/v1", "api_key": "k", "model": "glm-5.3",
                   "reasoning_policy": "keep", "preserved_thinking": True,
                   "reasoning_effort_map": GLM_CFG})
d = pool.list_endpoints()[0]
assert d["reasoning_policy"] == "keep" and d["preserved_thinking"] is True
assert d["reasoning_effort_map"] == GLM_CFG, d.get("reasoning_effort_map")

# 7) 回归护栏：不再有「关闭 thinking」注入（结构性无效 + 会污染轮转后的异构端点）。
#    严格校验 400 重试为**二级口径**（2026-09-12，外部实证 zdsub2api 后定案）：端点级上限
#    strict400_retries（0/1/2，默认 2）；第 1 级同端点原样重试 + 抖动退避，第 2 级
#    「原始 tool_call id」变体仅在本次确实应用过前缀重写时发；且只在**首包前**失败分支上重试。
#    2026-10-05：家族硬编码映射已迁到端点配置 reasoning_effort_map，关闭形态只允许由
#    端点配置驱动（无配置 = 不干预），仍禁止无条件注入。
src = open("/opt/data/work/api-pool2/api_pool_server.py", encoding="utf-8").read()
assert "disable_thinking_forced" not in src
assert "disable_thinking_eps" not in src
assert "_apply_reasoning_effort_map" in src, "关闭/等级映射由端点配置驱动"
assert "_GLM_EFFORT_MAP" not in src and "_DEEPSEEK_V4_EFFORT_MAP" not in src, "家族硬编码映射已删除"
assert src.count('payload["thinking"] = {"type": "disabled"}') == 1, "关闭形态只在配置分支里出现"
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
