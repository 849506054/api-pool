# API Pool 项目卡片

## 📋 项目卡片

| 字段 | 值 |
|------|------|
| **领域** | 基础设施 — DeepSeek API 端点集中管理 |
| **定位** | DeepSeek 端点集中管理工具，不再作为通用多模型聚合入口 |
| **当前阶段** | 功能维护 |
| **状态** | 🟢 运行中 |
| **源码** | /vol1/1000/tool/api-pool/ (宿主机) |
| **Git remote** | github.com/849506054/api-pool |
| **端口** | 5100 |
| **主链路** | Hermes 直连 custom:kcne + gpt-5.4-mini（不经过 API Pool） |
| **Fallback 链** | custom:api-pool → deepseek-v4-flash → custom:Ark → ark-code-latest |

## 🎯 里程碑

- [x] **v1.0 基础框架** — HTTP 反代 + 端点管理 + 健康检测
- [x] **手动切换端点** — /switch_to_endpoint API + Telegram 通知
- [x] **手动切换修复 (v3)** — _manual_override_id 字段与自动路由解耦
- [x] **探活统一为 chat ping** — 消除假阳性
- [x] **thinking reasoning_content 修复** — 空串→空格，全量补全
- [x] **瞬态故障冻结保护** — 30s 内 >5 次直接冻结
- [x] **UI 健康状态同步** — _on_success 更新 _health
- [x] **优先级插入模式** — update_endpoint 支持 insert-at-position 重排
- [x] **协议下拉框恢复** — commit 91110b1
- [x] **项目收口** — README / PROJECT.md 定位更新
- [ ] **清理陈旧备份文件** — 项目目录累积大量 .bak 文件待清理

## 📝 决策日志

| 日期 | 决策 | 理由 |
|------|------|------|
| 2026-06-27 | 单文件 Flask 应用 | 部署简单，su-exec 单进程 |
| 2026-06-27 | systemd 管理 + Restart=always | 原生服务管理 |
| 2026-06-29 | 新增 _manual_override_id | v2 修复在 failover 后被自动路由覆盖 |
| 2026-06-29 | 本地改→scp→验证→立即 commit+push | 用户强制要求的工作流 |
| 2026-06-30 | 探活统一为 chat ping | 原 pay_per_use 端点走 fetch_models 出现假阳性 |
| 2026-07-01 | thinking 注入空串→空格；全量补全所有 assistant 消息 | 故障转移时 disable_thinking 注入空串被 DeepSeek V4 拒绝 |
| 2026-07-01 | 瞬态故障冻结保护：30s 内 >5 次直接冻结 | 探活通过但请求持续失败→原地重试死循环 |
| 2026-07-01 | _on_success 更新 _health = "ok" | 假阳性探活后 _health 残留 bad |
| 2026-07-03 | update_endpoint 优先级改为 insert-at-position | 原逻辑会收拢覆盖手动优先级 |
| 2026-07-04 | 定位重新收口：DeepSeek 端点集中管理工具 | 多模型聚合与 Hermes model-name 决策机制冲突 |
| 2026-07-04 | 协议下拉框恢复为可见 select | 被回退为 hidden input，用户发现后恢复 |

## 📌 活跃事项

- [ ] **[P2] 清理陈旧备份文件** — 项目目录累积大量 .bak 文件
- [ ] **[P3] systemd 服务文件同步更新** — 确保证明文件与最新部署一致
