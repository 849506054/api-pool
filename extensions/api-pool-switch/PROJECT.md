# API Pool Switch

## 📋 项目卡片

| 字段 | 值 |
|------|-----|
| **领域** | Hermes 插件 — 命令 (slash command) |
| **性质** | 🔴 自有开发 |
| **目的** | 通过 `/endpoint` 斜杠命令在对话中列出、切换、健康检查 API Pool 端点 |
| **阶段** | 稳定运行 |
| **状态** | 🟢 v1.4.0 飞书交互卡片菜单 |
| **源码位置** | `api-pool2/extensions/api-pool-switch/`（运行副本同步至 `/opt/data/plugins/api-pool-switch/`） |
| **依赖项目** | Hermes Gateway（加载运行） |
| **对接服务** | API Pool 2.0（宿主机 192.168.5.6:5200） |
| **相关项目** | 同属 Hermes 自有插件生态 |

## 🎯 里程碑

- [x] **v1.0.0** — 基础功能：`/endpoint`、`/endpoint switch <n>`、`/endpoint check`、`/endpoint health`
- [x] 内联键盘选择器（Telegram 交互式端点列表）
- [x] v1.3.0 — 组选择器通过 API Pool `/api/pool/switch` 结构化接口切换（group + endpoint_id）
- [x] v1.4.0 — 飞书全卡片化：裸 `/endpoint` 发菜单卡片（池组 → 端点逐级按钮），`switch` / `check` / `health` 发结果卡片（标题配色随结果）；按钮点击由适配器转成 `/card` 合成命令回到插件处理
- [ ] 无活跃开发计划（已稳定，仅 API Pool 接口变更时需同步）

## 📝 决策日志

| 日期 | 决策 | 理由 |
|------|------|------|
| 2026-06-? | v1.0.0 定型 | 基础功能稳定，无后续需求 |
| 2026-06-30 | 补录决策日志 | PROJECT.md 规范化 |
| 2026-08-19 | 管理接口迁移至 API Pool 2.0（5200） | API Pool 1.0（5100）下线，保持 `/endpoint` 功能可用 |
| 2026-08-23 | 当前端点去掉后置 ✅ | 保留 picker 自动添加的前置 ✓；当前状态改为结构化 `is_current` 字段，不再从显示文本推断 |
| 2026-09-06 | 组感知切换改为 API Pool 项目扩展 | API Pool 提供 `/api/pool/switch`，插件只提交 `group + endpoint_id`；组归属、可切换条件、运行态持久化和结果响应由 API Pool 统一负责。插件源码与测试纳入 `api-pool2/extensions/api-pool-switch/`，生产加载目录由该扩展同步。 |
| 2026-10-06 | 飞书菜单走插件自建卡片，不改 Hermes 适配器 | 飞书适配器没有 `send_choice_picker`，但会把卡片按钮点击转成 `/card button {...}` 合成命令。插件复用适配器已连接的发送通道（`_feishu_send_with_retry` + `_finalize_send_result`，fry-cards 同款），并在 `pre_gateway_dispatch` 按 `apipool` 键认领点击，避免侵入 Hermes 源码。 |
| 2026-10-06 | 卡片点击授权改用适配器操作人口径 | 合成事件的 source 被适配器标成 `chat_type=group`（`_dispatch_synthetic_event` 硬编码 `event_chat_type="group"`），`gateway._is_user_authorized` 对它一律判否 → 点击后无反应。改用适配器自家卡片按钮同款判定 `_is_interactive_operator_authorized(操作人 open_id)`；适配器缺该接口时拒绝（fail closed）。与 fry-cards 修 approval 卡片点击被组策略误拒的处置同源。 |

## 📌 活跃事项

- [ ] **[P3]** API Pool 接口变更时同步 — 无定期检查机制，被动响应
