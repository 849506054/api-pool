# API Pool 2.0 阶段性成果总结

> 2026-08-18 | 阶段：Hermes 切流到 5200 + 配置适配完成，进入验收期

## 一、目标与范围

API Pool 2.0 是在 1.0 基础上，新增非 DeepSeek Endpoint 兼容能力的并行实验实例。核心目标：

- 池内可安全加入非 DeepSeek Endpoint
- 复用 1.0 的 Endpoint 管理、轮转、重试、冷却、defer 和终极兜底机制
- Hermes 继续使用固定的 OpenAI-compatible 聚合入口，无需改变接入方式
- 按目标 Endpoint 隔离 DeepSeek 专属字段，不污染非 DeepSeek 请求

**明确不做的范围**：ModelRoute、独立 Upstream、自动选模、成本调度、通用 reasoning 协议适配、Hermes agent loop 改造。

## 二、已完成工作

### 2.1 基础设施

| 事项 | 状态 |
|------|------|
| 独立目录 `/vol1/1000/tool/api-pool2` | ✅ |
| 独立 systemd unit `api-pool2.service` | ✅ |
| 端口 5200（1.0 继续 5100） | ✅ |
| 配置由 1.0 快照初始化 | ✅ |
| `chat_logs.db` / `token_stats.db` 独立新建 | ✅ |

### 2.2 非 DeepSeek Endpoint fallback 兼容

- 工作区实现后已推回到 5200 实例
- 模拟 DeepSeek 502 → OpenAI-compatible `gpt-5.6-sol` 切换验证通过
- 按目标 Endpoint 隔离 `reasoning_content` / `reasoning_text` 字段
- 非 DeepSeek 目标不收到无条件 DeepSeek 专属字段

### 2.3 Hermes 配置适配

| 配置项 | 操作 | 说明 |
|--------|------|------|
| `custom_providers.api-pool2` | 加 `api_mode: chat_completions` | 显式声明聚合入口协议 |
| `extra_body` | 移除 | 不再无条件注入 `thinking`/`reasoning_effort`，由 API Pool 按 Endpoint 隔离 |
| `model.context_length: 1048576` | 已配置 | 主压缩器路径已生效（自动探测路径仍 256K warning，不影响主会话） |
| `stale_timeout_seconds: 600` | 由 `providers.custom` 继承 | 不需要在 `custom_providers` 中重复配置 |

### 2.4 后端模型绑定修复

```
ep_model = model or ep.model  →  ep_model = ep.model
```

- 入口模型名 `api-pool` 不再覆盖真实上游模型
- 已验证：入口 `model: api-pool`，实际返回 `model: gpt-5.6-sol`
- 已推回宿主机部署并重启

### 2.5 Cron 快照迁移

10 个启用旧快照任务从 `custom/deepseek-v4-flash` 迁移到 `custom:api-pool2/api-pool`：
- 7 个信用卡还款提醒
- Agent 每周自检
- Hermes 版本更新每周预检
- 7 个已禁用旧快照任务不修改
- 3 个无快照任务继承全局配置，不需要修改
- 后续修正：`provider_snapshot` 应为 `custom` 而非 `custom:api-pool2`（运行时解析值为 `custom` 前缀）

### 2.6 Anthropic 流式响应转换（上游 cherry-pick）

| 事项 | 状态 |
|------|------|
| 上游 commit `9423819` cherry-pick 到 `api-pool-2.0` 分支 | ✅ |
| 合并冲突（3 处）解决，移除 `is_responses` 分支（2.0 无此协议） | ✅ |
| Anthropic 文本流转换（`content_block_delta` → `choices.delta.content`） | ✅ |
| Anthropic 工具流转换（`content_block_start[tool_use]` → `tool_calls`） | ✅ |
| Anthropic 结束事件（`message_stop` → `finish_reason` + usage + `[DONE]`） | ✅ |
| `finish_chunk()` 辅助函数，兼容 `stop`/`length`/`tool_calls` 原因 | ✅ |
| 新增 `AgentRouter-Anthropic` 端点（`claude-opus-4-8` / `protocol=anthropic` / `priority=4` / `in_pool=true`） | ✅ |
| 离线测试：6/6 通过（Anthropic 文本流/工具流/finish_reason/usage/DONE + OpenAI 透传回归） | ✅ |
| 已推回宿主机部署并重启，PID 2142989 | ✅ |
| 非流式实测：`model=claude-opus-4-8` `content=OK` `finish_reason=stop` | ✅ |
| 流式实测：`O`→`K`→`finish_reason=stop`→usage→`[DONE]`，model 正确 | ✅ |
| 工具流实测 | ⏳ AgentRouter 今日额度暂无 |

### 2.7 排障记录

- `provider_snapshot: custom:api-pool2` 触发 drift guard（运行时解析值为 `custom` 前缀，非 `custom:api-pool2`）→ 已修正为 `custom`
- 上游 `9423819` 是增量提交，依赖 `94c1764` 的 Anthropic 工具流转换；cherry-pick 时需同时提取 `content_block_start[tool_use]` 和 `input_json_delta` 逻辑
- `is_responses` 分支在 2.0 中无定义，合并后已移除
- `yield line` 在冲突合并时被误丢，重新恢复（否则 OpenAI 流式 SSE 透传失效）

## 三、当前运行状态（最终）

| 指标 | 值 |
|------|-----|
| 系统时间 | 2026-08-18 08:20 CST |
| 5200 实例 PID | 2142989 |
| 5200 启动时间 | 2026-08-18 08:18:45 CST |
| 端口 | 5200（1.0 继续 5100） |
| 端点总数 | 19 |
| 池内端点 | 5 |
| 健康端点 | 2 |
| 当前端点 | Tokenrhythm / deepseek-v4-flash |
| Hermes 主路由 | `custom:api-pool2` → 5200 |
| Cron 快照状态 | 11 个任务已迁移且验证通过 |

池内端点排序：

| 端点 | 优先级 | 模型 | 协议 | 健康 |
|------|--------|------|------|------|
| Tokenrhythm | 1 | deepseek-v4-flash | openai | ok |
| Opencode | 2 | mimo-v2.5 | openai | ok |
| AgentRouter | 3 | gpt-5.6-sol | openai | unknown |
| AgentRouter-Anthropic | 4 | claude-opus-4-8 | anthropic | ok |
| Kcne-gpt | 5 | gpt-5.6-sol | openai | unknown |

## 四、备份清单

| 目标 | 备份路径 |
|------|---------|
| `/opt/data/config.yaml` | `backups/config.yaml.bak.20260818_062927_apipool2_adapter` |
| `/opt/data/cron/jobs.json` | `backups/jobs.json.bak.20260818_065607_apipool2_cron_migration` |
| `/opt/data/cron/jobs.json` | `backups/jobs.json.bak.20260818_071736_provider_snapshot_fix` |
| `/opt/data/cron/jobs.json` | `backups/jobs.json.bak.20260818_071900_knowledge_sync_fix` |
| 宿主机 `api_pool_server.py`（模型绑定） | `/vol1/1000/tool/api-pool2/api_pool_server.py.bak.20260818_063431_hermes_adapter` |
| 宿主机 `api_pool_server.py`（Anthropic 流式） | `/vol1/1000/tool/api-pool2/api_pool_server.py.bak.20260818_081818_anthropic_stream_fix` |
| `PROJECT.md` | `backups/api-pool2-PROJECT.md.bak.20260818_070443` |
| 工作区 `api_pool_server.py`（scope 收窄前） | `backups/api_pool2-api_pool_server.py.bak.20260818_080756_anthropic_scope_fix` |

## 五、未完成待办

| 优先级 | 事项 | 说明 |
|--------|------|------|
| P0 | 真实端点验收矩阵 | 非 DeepSeek 流式/非流式/tools/带历史 reasoning 各场景；DeepSeek→非 DeepSeek 自动 fallback；冷却→恢复→defer；AllEndpointsFailed |
| P0 | Anthropic 工具流实测 | 等待 AgentRouter 额度恢复后测试 tools 请求 |
| P0 | Cron 2.0 链路验证 | 观察至少一次模型型 cron 实际运行，确认 5200 日志出现 cron 请求 |
| P1 | `/v1/models` 决策 | 增加标准路由或明确不支持 |
| P1 | 整理工作区 git 提交 | 将 `ep_model` 修复和 Anthropic 流式修复正式提交到 `api-pool-2.0` 分支 |
| P1 | 整理工作区探针脚本 | `probe_bisect.py`、`probe_content_blocked.py` 判断是否复用 |
| P2 | 观察期统计 | 成功率/fallback/冷却/延迟/cron 异常/缓存命中率 |

## 六、文档更新记录

| 文档 | 更新内容 |
|------|---------|
| `PROJECT.md` | 完整 2026-08-18 会话记录、决策日志、待办清单 |
| `api-pool-management SKILL.md` | 新增"已验证的 2.0 配置适配结论"段落 |
| `pool-testing-techniques-pitfalls.md` | 新增数据收集脚本参考和已知配置陷阱 |
| `collect_apipool2_stats.py` | 新脚本：5200 运行数据收集 |
| 本文档 | 阶段性成果总结，独立留档（含 Anthropic 流式修复） |
| `test/test_anthropic_streaming.py` | 新测试：Anthropic 文本流/工具流/OpenAI 透传回归 |
| `narrow_apipool2_anthropic_fix.py` | 临时修复脚本：移除 `is_responses` 分支 |