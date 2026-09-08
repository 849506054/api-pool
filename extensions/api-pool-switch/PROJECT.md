# API Pool Switch

## 📋 项目卡片

| 字段 | 值 |
|------|-----|
| **领域** | Hermes 插件 — 命令 (slash command) |
| **性质** | 🔴 自有开发 |
| **目的** | 通过 `/endpoint` 斜杠命令在对话中列出、切换、健康检查 API Pool 端点 |
| **阶段** | 稳定运行 |
| **状态** | 🟢 v1.3.0 组感知切换接口 |
| **源码位置** | `api-pool2/extensions/api-pool-switch/`（运行副本同步至 `/opt/data/plugins/api-pool-switch/`） |
| **依赖项目** | Hermes Gateway（加载运行） |
| **对接服务** | API Pool 2.0（宿主机 192.168.5.6:5200） |
| **相关项目** | 同属 Hermes 自有插件生态 |

## 🎯 里程碑

- [x] **v1.0.0** — 基础功能：`/endpoint`、`/endpoint switch <n>`、`/endpoint check`、`/endpoint health`
- [x] 内联键盘选择器（Telegram 交互式端点列表）
- [x] v1.3.0 — 组选择器通过 API Pool `/api/pool/switch` 结构化接口切换（group + endpoint_id）
- [ ] 无活跃开发计划（已稳定，仅 API Pool 接口变更时需同步）

## 📝 决策日志

| 日期 | 决策 | 理由 |
|------|------|------|
| 2026-06-? | v1.0.0 定型 | 基础功能稳定，无后续需求 |
| 2026-06-30 | 补录决策日志 | PROJECT.md 规范化 |
| 2026-08-19 | 管理接口迁移至 API Pool 2.0（5200） | API Pool 1.0（5100）下线，保持 `/endpoint` 功能可用 |
| 2026-08-23 | 当前端点去掉后置 ✅ | 保留 picker 自动添加的前置 ✓；当前状态改为结构化 `is_current` 字段，不再从显示文本推断 |
| 2026-09-06 | 组感知切换改为 API Pool 项目扩展 | API Pool 提供 `/api/pool/switch`，插件只提交 `group + endpoint_id`；组归属、可切换条件、运行态持久化和结果响应由 API Pool 统一负责。插件源码与测试纳入 `api-pool2/extensions/api-pool-switch/`，生产加载目录由该扩展同步。 |

## 📌 活跃事项

- [ ] **[P3]** API Pool 接口变更时同步 — 无定期检查机制，被动响应
