# Anthropic 兼容性验收矩阵

> API Pool 2.0 / 端口 5200。本文只记录已通过运行态或隔离回归验证的行为，不把“代码存在”当作“上游能力已确认”。

## 架构边界

对外接口仍是 OpenAI-compatible：

```text
Hermes /v1/chat/completions
    -> API Pool 2.0 /v1/chat/completions
    -> Anthropic endpoint /messages
```

API Pool 按端点 `protocol: anthropic` 做双向转换。API Pool 不提供原生入站 `/v1/messages`。

## 已真实验收

| 能力 | 状态 | 证据 |
|---|---:|---|
| 普通非流式文本 | ✅ | AgentRouter-Anthropic `claude-opus-4-8` 运行态请求 |
| 普通流式文本 | ✅ | SSE 文本、usage、`message_stop`、`[DONE]` |
| 非流式单轮工具调用 | ✅ | `tool_use` → `message.tool_calls` |
| 流式单轮工具调用 | ✅ | `tool_use`/`input_json_delta` → SSE `tool_calls` |
| 非流式多轮工具结果 | ✅ | `assistant.tool_calls` + `role=tool` → Anthropic `tool_use`/`tool_result` |
| 并行多个工具调用 | ✅ | 两个 `tool_use` 与两个 `tool_result` 均通过 |
| Anthropic → OpenAI 工具历史切换 | ✅ | 目标 OpenAI 端点完成后续回答 |
| OpenAI → Anthropic 工具历史切换 | ✅ | 目标 Anthropic 端点完成后续回答 |
| Hermes 实际工具循环 | ✅ | Hermes CLI 两轮 API call，terminal 工具执行后完成回答 |
| base64 PNG 非流式图片 | ✅ | 原生直连与 API Pool 均正确回答 `red` |
| base64 PNG 流式图片 | ✅ | SSE 文本、usage、单次 `[DONE]` |
| 显式 `thinking` 请求透传 | ✅ | 未提供时不注入，提供时转发到上游 |
| `thinking` 响应转换 | ✅ | `thinking_delta`/`thinking` → `reasoning_content`，隔离测试通过 |
| 流式提前 EOF 识别 | ✅ | 无 `message_stop` 时错误 chunk、`[DONE]`、失败计数、冷却 |

## 上游限制

上游 `/models` 声明 `claude-opus-4-8` 和 `claude-opus-5` 支持 Anthropic endpoint，但使用标准参数：

```json
{"thinking":{"type":"enabled","budget_tokens":1024}}
```

对两个模型的原生 `/messages` 探测均未返回 `thinking` block 或 `thinking_delta`。因此：

- API Pool 的 thinking 请求透传和响应转换已具备；
- 当前 `AgentRouter` 上游实际 extended thinking 输出未确认，不能宣称已支持；
- 不应无条件注入 thinking，以免普通请求增加 Token 成本。

## 图片边界

当前已验收的是 `data:image/...;base64,...`。普通远程 `https://` 图片 URL 不会被 API Pool 下载，而会转为文本占位：

```text
[Image URL: ...]
```

因此远程 URL 图片不计入已支持能力。

## 错误与恢复语义

- HTTP 400/401/403/429：当前请求返回错误，不在 `_try_endpoint` 内重复重试；外层可按池策略切换。
- HTTP 5xx、连接/超时：按端点 `max_retries` 重试，耗尽后交给外层故障处理。
- Anthropic 流在 `message_stop` 前提前 EOF：视为失败，记录错误、增加失败计数、设置冷却、发送错误结束块和 `[DONE]`。
- 测试结束后必须清理运行态错误并恢复活动端点；`clear-error` 不修改配置值。

## 不支持或未确认

- API Pool 原生入站 `/v1/messages`：不支持。
- 上游实际 extended thinking 输出：未确认。
- 远程 URL 图片自动下载：不支持。
- 所有 Anthropic 专有参数的完整等价映射：未承诺。

## 相关实现

- 端点协议转换：`api_pool_server.py` 的 `_try_endpoint()` Anthropic 分支
- 流式 EOF 保护：Anthropic `stream_generator()` 的 `message_stop` 完整性判断
- API Pool 运行实例：宿主 `/vol1/1000/tool/api-pool2/`，端口 `5200`
