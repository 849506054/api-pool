# 端点错误分类与处置

## 分类入口

错误文本的语义和请求所在路径共同决定处置。`APIPool._classify_capacity_error` 先识别余额、额度、配额与敏感词；`_classify_retryable_stream_error` 识别首包限流和临时风控；`_classify_client_error` 识别请求形状类错误。HTTP 壳与内部文案一起保留，首包 SSE 提取覆盖嵌套 `error`、顶层 `message` / `code` 与 `response.error`。

新增 marker 按中英文同义词及形态变体铺全，例如额度 / 配额 / 余额，以及 `quota_exhausted` / `quota exhausted`；用真实报文和相邻类别对照验证。

## 现行处置

| 类型 | 重试与路由处置 |
|---|---|
| 连接、首包超时、HTTP 5xx、假成功 | 在端点次数和请求预算内按相应路径重试；耗尽后 `_rotate` 记失败并按端点 `cooldown_minutes` 冷却、轮转 |
| HTTP 429 或首包 `rate_limit` | `retry_on_rate_limit` 开启时先原端点退避重试；关闭或次数耗尽后进入既有冷却/轮转 |
| 首包 `temporary_policy_block` | 常规重试，由 `max_retries`、`force_no_retry` 与 `request_deadline` 限制 |
| 空响应、首帧前空流 | 先在原端点常规重放，耗尽后返回可重试错误并走失败处置 |
| 客户端类 400 / 404 / 413 / 422 | 记失败、按端点冷却（含抖动与阶梯）、交棒；请求内记录已尝试端点，候选探活按该类既有分支跳过 |
| `balance_insufficient` | 手动解冻标记，保持冻结至用户处理 |
| `sensitive_words` | 命中上游确定性敏感词，保持冻结至手动解冻，请求转其他候选 |
| `quota_out` | 默认冷却 24 小时，有恢复时间时取其值，冷却期间退出候选与探活 |
| `quota_exceeded` | 默认冷却 5 小时，有恢复时间时取其值 |
| 单请求饿死 `starvation_handback` | 本次失败交回调用方，端点状态与组指针保持；请求级挤压独立于组耗尽 |
| 已出站尝试被预算截断 `budget_cut` | 记失败并按端点冷却，容量类仍取容量冷却；组内轮转受剩余预算约束 |
| 预算已尽、尚未出站的候选 | 调用方侧收尾，返回最后错误，端点失败计数与冷却保持 |
| 池侧凭据脱敏失败 | 503 与同一错误码，零新出站，端点失败计数保持；见 [脱敏契约](outbound-credential-redaction-2026-11-08.md) |

`_set_capacity_cooldown` 的纯限流 Retry-After 分支以错误串中的 `HTTP 429` 为入口；502 壳的限流签名可参与开关重试，最终冷却仍沿用通用阶梯。普通冻结由 `_set_cooldown` 处理抖动、连续失败阶梯和幂等；迟到成功保留仍有效的冷却。

## 额度、配额与余额

`quota_out` marker 包括 `额度耗尽`、`额度已用尽`、`额度不足`、`quota_exhausted`、`quota exhausted`、`quota has been exhausted`、`credit exhausted`、`no remaining credit`。`manual_unlock_required=false`，冷却到期自动走恢复链；`health_mode=none` 的端点到期直接恢复。

`insufficient balance` 等余额类属于 `balance_insufficient`；`quota exceeded` / `daily limit reached` / `monthly limit reached` 等属于 `quota_exceeded`。数字 112 的真实报文含额度签名，按完整文案分类。

## 客户端类与池自身测试

客户端业务的请求形状失败走标准冻结链，冷却到期恢复并消费实际故障交接登记；缓存保护可能延迟回迁。相同畸形请求可能依次冻结多个候选，修正形态是消除该代价的入口。

池自身 `/api/test` 使用伤害隔离：非容量类错误只更新 `last_error` 等观测态，容量类仍按对应容量规则处置。真实业务与池自身测试各自按路径判定。

## 流事务与证据

首包前可以在预算内重放；有效输出后的停滞/超限以可见错误 finish 和截断标记交给客户端，已输出内容保持一次交付。流事务中止与 `_rotate` 端点失败记账按实际代码路径分别判断。

小 ping 通过只证明该请求形态通过。403/405 的 HTML 边缘拦截、客户端登记拒绝或身份拒绝，须按原始正文区分。失败摘要 `_outbound_digest` 只在 DEBUG 开启时生成规模与项数，正文和凭据由私有记录承载。

## 正式测试入口

- `test/test_client_error_classification.py`、`test/test_budget_cut_retry.py`、`test/test_budget_exhausted_no_cooldown.py`
- `test/test_retry_on_rate_limit.py`、`test/test_response_empty_guard.py`、`test/test_retry_recovery_scope.py`
- `test/test_stream_timeout_policy.py`、`test/test_business_stall_detection.py`、`test/test_failover_lock_free.py`
- `test/test_outbound_digest.py`、`test/test_runtime_log_noise.py`

选择与运行方式见 [testing](../testing.md)。线上自然 112 触发的证据缺口见 [PROJECT](../../PROJECT.md)；事故、本机诊断与旧矩阵完整来源保留在本地档案。
