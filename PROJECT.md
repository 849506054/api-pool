# API Pool

## 项目定位

这是自有开发项目，源码由本仓库管理，维护技能为 `api-pool-management`。API Pool 是多模型 API 端点聚合网关。客户端使用 OpenAI 兼容 Chat / Responses 入口；网关以一端点一模型为配置单元，按组选择、重试和故障转移，并桥接四种上游协议。

## 当前阶段

功能维护。API Pool 2.0 是唯一正式运行实例，正式分支为 `main`。当前决策与维护入口在本仓库；部署路径、服务参数、运行文件读数和私有配置的唯一来源是维护技能 `api-pool-management/references/deployment-info.md`。

## 活跃事项

本卡片截至 2026-11-09 的原业务待办均已完成。当前授权事项为项目资料规范试点：整理与验证完成，五类查找实测后的修正已复核，等待用户验收；交付说明在本地 `workspace/records/documentation-pilot-20261109/DELIVERY.md`，验收后提交。后续业务工作从用户批准的改动或实际故障样本进入。

最近验收：2026-11-08 页面 gzip、ETag 条件 GET 与管理状态 JSON 压缩已部署并验收；用户确认手机数据流量操作与内网体感接近。请求凭据脱敏已上线，开关的实时值由管理接口读取。

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

| 事项 | 已有证据 | 仍需观察的部分 |
|---|---|---|
| 对话日志写路径，2026-11-07 | 隔离回归通过；部署后真实请求同秒入库且接口可读 | 完整线上清理自然周期的写入表现 |
| 额度耗尽 `quota_out`，2026-11-05 | 真实 112 报文离线分类与部署文件复算均为 24 小时；启动正常 | 首次线上自然 112 触发、冷却与到期恢复链路 |

这些是证据缺口，后续按自然样本补证；本次文档整理的检查只覆盖文档结构、引用和来源保留。

## 版本与历史

2026-11-09 整理基线为 `main` / `c95dad8`（历史定位锚点）。后续版本以仓库 Git 读数为准，生产版本以维护技能的运行文件读数为准。

[历史入口](docs/history/README.md) 按日期与主题整理决策，并指向完整本地原文和 Git 追溯方式。早期阶段设计在历史目录按角色保留；现行机制正文归 [contracts](docs/contracts/README.md)。
