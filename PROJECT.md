# API Pool

## 项目定位

这是自有开发项目，源码由本仓库管理，维护技能为 `api-pool-management`。API Pool 是多模型 API 端点聚合网关。客户端使用 OpenAI 兼容 Chat / Responses 入口；网关以一端点一模型为配置单元，按组选择、重试和故障转移，并桥接四种上游协议。

## 当前阶段

功能维护。API Pool 2.0 是唯一正式运行实例，正式分支为 `main`。当前决策与维护入口在本仓库；部署路径、服务参数、运行文件读数和私有配置的唯一来源是维护技能 `api-pool-management/references/deployment-info.md`。

## 活跃事项

本卡片截至 2026-11-09 的原业务待办均已完成。当前授权事项为项目资料规范试点：整理与验证完成，五类查找实测后的修正已复核，等待用户验收；交付说明在本地 `workspace/records/documentation-pilot-20261109/DELIVERY.md`，验收后提交。后续业务工作从用户批准的改动或实际故障样本进入。

最近验收：2026-11-09 三项已部署并经用户实测验收——🧪 测试端点改为单次出站（不再按 `max_retries` 重放）；保存端点时 `token_stats` 的 `database is locked` 按「索引 + 改名分批 + 写锁冲突短退避重试」修复；保存按钮点击即禁用显示「保存中…」并在途去重。交付记录见 `workspace/records/test-noretry-token-lock-20261109/DELIVERY.md`。
此前同日验收：new-api 账号额度不足归入 `quota_out`（403 不再按 1 分钟短冻结反复重撞）；子组加入父组后新增本组停用 ⏸/▶，停用只退出该父组轮转，组内端点状态与其他父组不动；子组整组冻结时池卡条目补 ⏰，逐个解除冻结成员（复用端点 clear-error）。交付记录与回滚快照见 `workspace/records/subgroup-disable-and-quota-split-20261109/`，⏰ 条目见 `workspace/records/subgroup-unfreeze-20261109/DELIVERY.md`。此前 2026-11-08 页面 gzip、ETag 条件 GET 与管理状态 JSON 压缩已验收；请求凭据脱敏已上线。

## 按任务路线

| 任务 | 入口 |
|---|---|
| 接入与日常使用 | [README](README.md) |
| 理解架构和当前行为 | [架构](docs/architecture.md)、[现行契约](docs/contracts/README.md) |
| 改代码与新增字段 | [文档导航](docs/README.md)、[测试选择](docs/testing.md) |
| 部署与维护 | 维护技能 `api-pool-management` 的 `development-workflow.md` / `operations.md`；环境值见 `references/deployment-info.md` |
| 排障与观察 | [错误分类](docs/contracts/endpoint-error-classification.md)、[测试与证据分层](docs/testing.md)；现场操作走维护技能 |
| 查版本和决策历史 | [历史入口](docs/history/README.md)、Git 历史 |

## 验收与观察缺口

当前没有挂起的观察项。2026-11-07 的对话日志清理已跑过完整自然周期（当日 7 天保留清理删 988 条与 2 条，每小时 prompt 截断连续执行，无报错）；2026-11-09 的 `quota_out` 自然样本按用户决定不再跟踪，判断有误时再从交付记录补证。

## 版本与历史

2026-11-09 整理基线为 `main` / `c95dad8`；同日功能收尾提交 `99ddea0`，池卡子组 ⏰ 解冻 `1e34eff`，测试端点单次出站与 token_stats 写锁修复 `247d081`。后续版本以仓库 Git 读数为准，生产版本以维护技能的运行文件读数为准。

[历史入口](docs/history/README.md) 按日期与主题整理决策，并指向完整本地原文和 Git 追溯方式。早期阶段设计在历史目录按角色保留；现行机制正文归 [contracts](docs/contracts/README.md)。
