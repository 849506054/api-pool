# 当前架构

本页描述 API Pool 2.0 的组件关系。来源是 `api_pool_server.py`、`credential_redaction.py` 与 `static/index.html` 的符号接线；运行效果由各契约的测试和部署证据确认。

## 配置与路由单位

`Endpoint` 将连接、固定 `model`、上游 `protocol` 与端点行为参数绑定在一起，一端点一模型。同站点的多个模型仍用不同端点表示。

`APIPool` 维护组定义 `pool_group_defs`、组内优先级 `priority_by_group`、当前指针与运行态。请求先匹配组选择器或组名，未命中时走 main；`/v1/models` 暴露组选择器及显式 `context_tokens`。mixed 组承载异构端点，dedicated 组提供绑定模型的对外选择器；成员配置由操作者管理。

main 与 vision 是内置组。普通组可引用专用组作为一个完整名次；父组保存各自的名次与指针，多个父组共享子组内部当前端点。`_group_axis_slots` 定义名次轴，`_parent_slot_routes` 按轴调度并在名次内走成员环，`_resolve_subgroup_current` 下钻。细则见 [子组引用契约](contracts/subgroup-join-main-contract.md)。

配置式 `/endpoints/<id>/v1` 定向入口绑定已保存端点，复用协议桥与参数；它独立于组选择器，日志池组为 `apipool`。

## 请求链

```text
客户端 Chat / Responses
  → Handler：解析、客户端上下文、入口协议转换
  → ContentFilter.filter_payload：入口内容规则
  → APIPool.chat：组选择、上下文检查、图片兼容、尝试与重试
  → _prepare_endpoint_payload：目标端点参数与消息形态
  → _try_endpoint：四协议桥接、出站身份、凭据脱敏副本、send
  → 响应解码与流事务 → 客户端响应、usage 与本地日志
```

入口过滤修改请求本体，改后内容贯穿日志和后续尝试；凭据脱敏只修改每次发送前的出站副本，原请求与本地日志保持原值。两者各有配置和职责。

## 四协议桥

| 上游 `protocol` | 代码入口与职责 |
|---|---|
| `openai` | `_try_endpoint` 的 Chat 请求与 SSE/JSON 响应路径 |
| `anthropic` | `_try_endpoint` 协议分支；`_anthropic_tools_from_chat` / `_anthropic_tool_choice_from_chat` 处理工具结构 |
| `responses` | `_responses_body_from_chat` 与 Responses 事件/usage 转换 |
| `gemini` | `_gemini_payload_from_chat` 与 Gemini 响应转换；工具续轮保留 `thoughtSignature` |

上游协议由端点配置显式选择。客户端入口形态与上游协议可以不同；流式已开始输出后的失败按协议交付可见错误，透明重放边界见 [错误分类](contracts/endpoint-error-classification.md)。

## 身份、过滤与脱敏

`resolve_outbound_headers` 按 `client_profile` 选择透传或 profile，池托管鉴权与连接级字段由网关处理；`_apply_profile_body_identity` 在协议桥后应用合法身份字段。`_DecodedResponse` 增量解压响应。`_guard_pool_outbound` 约束池自身出站，`health_mode=none` 只允许客户端业务。细则见 [身份契约](contracts/client-identity-and-outbound-policy.md)。

`ContentFilter.filter_payload` 处理 literal / regex / zero_width 入口规则。`credential_redaction.redact_body` 处理明确凭据字段与格式，开关每次 send 读取；失败走 `CredentialRedactionError` 的 fail-closed 路径。见 [凭据脱敏](contracts/outbound-credential-redaction-2026-11-08.md)。

## 状态、存储与管理页

`_set_current` 与 route epoch 管理并发回写；`_recovery_groups` / `_recovery_slots` 记录故障交接和恢复资格。`_rotate`、`_background_probe` 与 `_reconcile_deferred` 分别负责失败、恢复和等待释放。[手动切换](contracts/manual-switch-cooldown-noop-2026-09-06.md)、[轮换](contracts/group-rotation-contract-2026-10-05.md)、[空闲借用](contracts/idle-borrow-contract-2026-09-18.md) 各有独立触发条件。

`_endpoint_config_record` 从数据类推导配置字段，`save_config` 原子替换配置；runtime_state 保存可恢复运行态。TokenTracker / ChatLogger 保存 usage 和对话记录。管理页使用组与聚合链的同源状态渲染，页面 mtime 热读取与 HTTP 传输见 [页面契约](contracts/ui-http-transfer.md)。

本页的确认范围是仓库符号、字段与路由接线；生产 hash、端点成员、凭据、环境参数和实时开关由维护技能的第一方读数承载。
