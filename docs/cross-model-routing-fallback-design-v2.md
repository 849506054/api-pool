# API Pool 2.0 跨模型路由与故障转移设计稿

> 状态：**历史过度设计，2026-08-18 已废弃，不得作为实现依据。** 当前唯一有效设计为 `docs/minimal-non-deepseek-fallback-design-v1.md`；本文件仅保留审计记录。文中的 1.0/2.0 并行描述是当时状态；API Pool 1.0 已于 2026-08-21 彻底删除。
>
> 目标：在 API Pool 2.0 中复用 1.0 已验证的端点轮转原语，并参考 Hermes fallback 的 `(provider, model)` 有序链，实现可控的跨端点、跨模型故障转移，同时保持请求上下文和会话连贯性。

## 1. 结论

API Pool 2.0 不应复制 Hermes 的整个 agent fallback 状态机，而应复用其中三个已验证的设计原则：

1. **路由目标必须绑定“连接 + 模型”**，不能只切端点或只改模型名。
2. **fallback 是显式有序链**，只有配置过的目标才允许跨模型切换。
3. **每次请求从主路由重新开始**；失败目标的冷却状态可跨请求保留，但 fallback 选择本身不永久篡改主模型。

API Pool 与 Hermes 的职责不同：Hermes 持有会话和 agent loop；API Pool 是无状态请求代理。因此，会话历史仍由 Hermes 保证，API Pool 只负责在一次尚未向客户端提交输出的请求内，安全地重放同一份完整 payload。

## 2. 设计边界

### 2.1 本设计负责

- 同一模型在多个端点之间轮转。
- 主模型不可用时，按显式链切换到其他模型和端点。
- 每个端点独立配置，功能默认关闭。
- 保留 1.0 的 priority、重试、冷却、defer、终极兜底原语。
- 对流式与非流式请求定义明确的切换边界。
- 为后续 reasoning 协议适配提供目标模型信息。

### 2.2 本设计不负责

- 根据任务复杂度主动选择强弱模型。
- 修改 Hermes 的 `/model`、agent loop 或 fallback 配置。
- 在已经输出部分流式内容后跨模型重放。
- 本阶段实现 `/v1/responses`。
- 自动推断一个模型失败后“应该换成哪个模型”。

主动选模属于后续策略层；本设计先完成确定性路由和故障转移底座。

## 3. 与现有方案的关系

保留已拍板部分：

- `Endpoint` 继续表示上游连接、凭据和端点级运行状态。
- `Endpoint.model_routes` 表示该连接提供的模型能力。
- 未配置 `model_routes` 的端点继续使用 `endpoint.model` 旧语义。
- 端点尝试继续复用 `_try_endpoint()`、`_rotate()`、cooldown、defer 和 priority。

修订旧稿部分：

- 旧稿只定义“请求模型命中后，在同名模型候选间轮转”。
- v2 增加显式跨模型 fallback chain；同名候选全部失败后，不必立即 `AllEndpointsFailed`。
- 旧稿的“未匹配就退回任意旧端点”只保留给完全未启用多模型路由的旧端点路径。进入显式模型路由后，不允许把未知模型静默发给无关端点。

最后一项是必要的边界收紧：显式 route 模式若仍把未知模型随机落到旧端点，路由结果不可预测，也无法保证协议适配正确。

## 4. 数据模型

### 4.1 Endpoint 增量

```json
{
  "model_routing_enabled": false,
  "model_routes": []
}
```

- `model_routing_enabled`：端点级总开关，默认 `false`。
- `false` 时完全走 1.0 单模型语义，不读取新增路由配置。
- 这是每端点独立配置，不设置全局强制开关。

### 4.2 ModelRoute

```json
{
  "model": "gpt-5.6-sol",
  "enabled": true,
  "priority": 0,
  "reasoning_protocol": "passthrough",
  "vision": false,
  "max_context_k": 0,
  "fallback_enabled": false,
  "fallback_chain": [
    {
      "model": "deepseek-v4-flash",
      "endpoint_id": "opencode",
      "on": ["capacity", "auth", "not_found", "invalid_response"]
    }
  ]
}
```

字段语义：

- `model`：该连接实际接受的上游模型 ID。
- `priority`：同一 Endpoint 内 route 顺序；跨 Endpoint 仍以端点 priority 为第一排序键。
- `reasoning_protocol`：本阶段仅建模，默认 `passthrough`，不立即改写 wire payload。
- `fallback_enabled`：route 级开关，默认 `false`。
- `fallback_chain`：有序目标列表，语义对应 Hermes 的 `fallback_providers`。
- `endpoint_id`：可选。指定时绑定到确切连接；省略时匹配所有提供该 model 的启用 route，并按 priority 排序。
- `on`：允许触发该目标的失败类别，避免把请求格式错误误判为容量故障。

route 级开关比全局链更符合“每端点独立配置、默认关闭”的既有偏好，也允许同一个上游的不同模型采用不同 fallback 策略。

## 5. 路由运行单元

内部统一使用不可变运行单元：

```text
RouteTarget = (endpoint_id, model, route_config)
```

一次尝试必须同时确定 Endpoint 和 model。禁止以下两种隐式行为：

- 已切 Endpoint，但继续沿用上一个端点的固定 model。
- 已改 payload.model，但仍使用不支持该模型的任意 Endpoint。

每次尝试从原始请求深拷贝生成独立 payload，再应用目标 route 的 model 和协议适配。不得原地修改共享 payload，否则第一次尝试删除或补写的 reasoning/tool 字段会污染后续模型。

## 6. 路由算法

### 6.1 主路径

1. 读取 `body.model`，不得再在 API 入口剥离。
2. 查找所有 `model_routing_enabled=true` 且 route.model 精确匹配的 `RouteTarget`。
3. 候选按 `(endpoint.priority, route.priority)` 排序。
4. 对同名模型候选逐个复用 1.0 尝试原语。
5. 任一成功立即返回；所有同名候选失败后，才评估 fallback chain。

### 6.2 跨模型 fallback

1. 以实际主 route 的 `fallback_chain` 为准，不使用全局猜测。
2. 按链顺序解析每个 fallback target。
3. 仅当失败类别包含在该 target 的 `on` 中时才尝试。
4. 每个 target 可再次展开为一个或多个同名模型候选端点。
5. 一个请求内维护 `visited={(endpoint_id, model)}`，禁止环和重复尝试。
6. 链耗尽后抛出 `AllEndpointsFailed`，错误中保留完整 route trace。

第一版不支持 fallback route 再递归继承自己的 fallback chain。只执行主 route 的一层有序链，避免配置图形成循环和不可控放大。确有需要再扩展为有最大深度的 DAG。

### 6.3 未匹配行为

- 若池中没有任何端点启用 `model_routing_enabled`：完全走 1.0 旧路径，保持兼容。
- 若请求已进入显式 route 模式，但 `body.model` 没有 route：返回明确的 `model_not_routed` 错误，不随机落到旧端点。
- 若请求未提供 model：继续使用当前默认/手动覆盖端点的 `endpoint.model`，不触发跨模型 fallback。

这样把存量兼容和显式多模型语义分开，不让新旧规则互相污染。

## 7. 失败分类

参考 Hermes fallback 的触发范围，但复用 API Pool 现有错误检测结果：

| 类别 | 典型情况 | 默认允许跨模型 |
|---|---|---|
| `capacity` | 429、500、502、503、504、连接/读取超时 | 是 |
| `auth` | 401、403 | 是，但应同时标记端点凭据异常 |
| `not_found` | 404、上游明确 model unavailable | 是，优先 route 级冷却 |
| `invalid_response` | 空响应、Malformed JSON、缺失 choices | 是 |
| `request_invalid` | 400、422、tool/schema/message 顺序错误 | 否 |
| `policy` | 内容审核、敏感词、供应商策略拒绝 | 默认否，避免绕过策略或扩大请求 |
| `client_cancelled` | 客户端主动断开 | 否 |

不能把所有异常都当 fallback 条件。尤其 400/422 通常由同一 payload 引起，盲目换模型只会重复失败或产生语义漂移。

## 8. 冷却与健康状态

多模型端点不能继续只有端点级健康状态，否则一个模型 404 会冻结整个连接上的其他模型。

### 8.1 route 级状态

新增仅运行态字段：

```text
RouteRuntimeState(endpoint_id, model):
  fail_count
  last_error
  cooldown_until
```

- model unavailable、模型级限流、invalid response：优先冷却 route。
- 连接超时、DNS、TLS、端点 5xx、凭据失效：沿用 Endpoint 级冷却。
- 成功只清理对应 route 状态；端点级状态仍按 1.0 规则处理。

### 8.2 回迁

fallback 选择是请求级的；下一请求仍从原始 model 的主候选开始。若主 route 尚在冷却，则跳过并继续 fallback；冷却到期后自然恢复主 route。

这与 Hermes“每 turn 从 primary 开始、reset 未到则跳过”的效果一致，但不要求 API Pool 持有会话状态。

## 9. 会话连贯性与流式边界

### 9.1 为什么跨模型不会天然丢会话

Hermes 每次请求都会发送当前完整消息历史、工具调用和上下文。API Pool 不保存对话，只把同一原始 payload 重放给另一个 `RouteTarget`。因此更换端点和模型不会自动丢失会话。

### 9.2 连贯性成立的前提

- 目标模型支持当前工具 schema 和消息角色。
- reasoning/history 字段已按目标协议独立适配。
- fallback 发生在客户端尚未收到本次响应内容之前。
- API Pool 不复用第一次失败尝试中已被修改的 payload。

### 9.3 流式硬边界

- **首个下游字节发出前失败**：允许 fallback。复用现有首包预读窗口，在确认上游首个有效 chunk 后才提交响应头/内容。
- **已经向客户端输出任意 chunk 后失败**：禁止跨模型重放。此时重放会产生重复文本、断裂 tool_call 或两个模型拼接的响应。
- 已提交流发生故障时，按现有流式错误路径结束，并由 Hermes 决定下一轮重试/fallback。

不引入全量流缓冲。全量缓冲会破坏实时输出并增加内存和首字延迟，收益不足。

## 10. 响应与可观测性

- 非流式响应和每个流式 chunk 的 `model` 应反映实际命中的 fallback 模型。
- 日志增加一个请求级 route trace：

```text
request_model=gpt-5.6-sol
attempts=[agentrouter:gpt-5.6-sol, opencode:gpt-5.6-sol, opencode:deepseek-v4-flash]
selected=opencode:deepseek-v4-flash
fallback_reason=capacity
```

- `chat_logs.db` 增加 `requested_model`、`actual_model`、`endpoint_id`、`fallback_depth`、`failure_class`。
- UI 当前端点状态之外，增加 route 状态和最近 fallback 原因；不把 route 冷却误显示为整个端点离线。
- 错误响应返回聚合摘要，不把 API key、完整 payload 或供应商敏感正文写入日志。

## 11. UI 与配置交互

Endpoint 编辑面板增加折叠的“模型路由”区域：

- `启用多模型路由` 开关，默认关闭。
- 从 `/api/fetch-models` 导入模型目录，只生成 route 草稿，不自动启用 fallback。
- 每个 route 可配置 enabled、priority、reasoning protocol、fallback 开关与有序链。
- fallback 目标使用模型选择器 + 可选端点选择器，禁止自由文本引用不存在的 endpoint_id。
- 保存前校验重复目标、自引用、空 model、禁用 route 引用和不存在端点。

## 12. 分阶段实施

### Phase A：数据模型与只读能力

- 增加 `model_routing_enabled`、`ModelRoute` 和序列化白名单。
- 增加 route 配置校验与 `/api/fetch-models` 导入预览。
- UI 只展示和编辑配置，不接管请求路由。
- 验证存量 config 加载后字节级语义不变。

### Phase B：同模型跨端点路由

- `body.model` 参与精确匹配。
- 只实现同名模型候选轮转，不启用跨模型链。
- 验证 stream、non-stream、tools、vision 和手动 override。

### Phase C：显式跨模型 fallback

- 实现失败分类、visited 集合和一层 fallback chain。
- 增加 route 级运行状态。
- 严格执行首字节前 fallback 边界。

### Phase D：reasoning 协议适配

- 根据最终 `RouteTarget` 转换 `thinking`、`reasoning_effort` 和历史 reasoning 字段。
- 每次尝试从原始 payload 构造，覆盖 DeepSeek、OpenAI、Anthropic 三类协议。

### Phase E：真实流量验证与切换评审

- 在 5200 实验实例上用 AgentRouter、Opencode、Tokenrhythm 建矩阵。
- 1.0 的 5100 保持运行，不重启、不切流。
- 通过验收后单独提交 Hermes 是否切向 5200 的设计决策，不随代码完成自动切换。

## 13. 验收矩阵

至少覆盖：

1. 未启用新功能的存量 Endpoint 行为与 1.0 一致。
2. 同一 model 在两个端点间按 priority 轮转。
3. 主 model 429/5xx/超时后按显式链切到另一 model。
4. 主 model 400/422 时不跨模型。
5. route 404 只冷却该 route，不冻结同端点其他 model。
6. 端点连接错误冻结 Endpoint，并跳过其全部 routes。
7. fallback 链自引用、重复和不存在目标保存失败。
8. 非流式 fallback 返回 actual model。
9. 流式首包前失败可 fallback，首 chunk 后失败禁止重放。
10. tool_call id、assistant/tool 顺序和 reasoning 字段在跨模型尝试中不被前一次 payload 污染。
11. 下一请求优先重试主 route；冷却未到时跳过，到期后自动回迁。
12. 终极兜底与显式跨模型链的顺序确定且不会循环。

## 14. 关键决策摘要

- **采用**：`Endpoint + ModelRoute`，不复制凭据造伪端点。
- **采用**：route 绑定 Endpoint 与 model。
- **采用**：显式有序 fallback chain，默认关闭、每 route 独立配置。
- **采用**：同名模型候选先尝试，随后才跨模型。
- **采用**：请求级 fallback + 持久冷却 + 自动回迁。
- **采用**：首个下游字节前才允许流式 fallback。
- **采用**：route 级与 endpoint 级健康状态分离。
- **不采用**：未知模型随机投递到任意旧端点。
- **不采用**：所有错误无差别跨模型。
- **不采用**：递归 fallback 图、全量流缓冲、任务复杂度主动选模。
