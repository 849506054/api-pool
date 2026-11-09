# 端点重试开关与首包 SSE 分类

## 开关与分类

`Endpoint.retry_on_rate_limit=False` 只控制 HTTP 429 与首包 SSE 的 `rate_limit`。开启后按端点 `max_retries` 在原端点退避；关闭则进入既有故障处置。

`_classify_retryable_stream_error` 先用 `_classify_capacity_error` 排除余额、配额、额度和确定性敏感词，再识别：

- `rate_limit`：`too_many_requests` / `rate limit` / `rate_limit` / `429` / `retry-after`。
- `temporary_policy_block`：完整签名 `your resource has been temporarily blocked because we detected behavior that may violate our content policy`，大小写不敏感。
- 未识别类别返回空字符串。

临时风控、空响应与首帧前空流走常规重试，受 `max_retries`、`force_no_retry` 与 `request_deadline` 约束。新增可重试签名在分类入口登记，调用点仅对 `rate_limit` 检查开关。

## 执行边界

预读失败先关闭响应并保存错误，退出预读块后才退避、继续外层尝试。重放使用原请求体；输出开始后的中流错误按流事务处理。

`_retry_backoff(attempt, category)` 共用 `RETRY_BACKOFF_CAP_SECONDS`，当前曲线为 3/6/12/24/24… 秒；类别标注保持，退避曲线统一。限流、HTTP 5xx、连接超时、假成功、首包超时、空流和空响应调用同一函数，次数仍受端点配置与剩余预算限制。

已出站失败的重试放不进预算时，`retry skipped: request budget exhausted` 按普通失败记账、冷却，容量类保持容量规则；预算已尽且候选尚未发送时保持端点状态。现行处置见 [错误契约](endpoint-error-classification.md)。

首包 INFO 携带类别、请求 ID、端点、等待时间与次数，与同请求成功及流式收尾配对。真实上游仅给裸 `error` 时按未识别签名处理。

## 正式测试入口

`test/test_retry_on_rate_limit.py` 覆盖开关、次数、预算、容量排除、签名误匹配、响应关闭和 Chat/Responses 首包实线；`test/test_budget_cut_retry.py` 覆盖退避与预算截断；`test/test_response_empty_guard.py` 覆盖空响应重放。支持 `API_POOL_TEST_SOURCE` 的测试可对指定代码文件复算，详见 [testing](../testing.md)。
