# 文档导航

从任务进入。项目当前阶段与观察缺口见 [PROJECT](../PROJECT.md)，用户接入说明见 [README](../README.md)。

| 任务 | 先读 | 继续定位 |
|---|---|---|
| 接入与使用 | [用户说明](../README.md) | 组选择器、端点定向调用与管理页用法 |
| 理解设计 | [架构](architecture.md) | [现行契约索引](contracts/README.md) |
| 开发与配置字段扩展 | [测试选择](testing.md) | [字段接线](contracts/endpoint-field-addition-checklist.md)、对应行为契约；实现授权与本机流程见维护技能 `development-workflow.md` |
| 验证与验收 | [测试与证据分层](testing.md) | 直接命中的测试文件、隔离实线、部署文件复算与用户验收 |
| 部署与故障处理 | [错误分类](contracts/endpoint-error-classification.md) | 维护技能 `api-pool-management` 的 `operations.md`、`development-workflow.md` 与 `references/deployment-info.md` |
| 查版本和历史决定 | [历史入口](history/README.md) | 按主题归档的原文、Git 路径追溯与本地完整项目记录 |

## 资料归属

仓库 `contracts/` 保存现行设计、接口和行为正文；`architecture.md` 说明组件关系；`testing.md` 按改动类型选择验证。维护技能保存环境、账号、现场操作与排障配方，并指向仓库契约。本地 `workspace/` 保存完整旧记录、私有证据和本次迁移清单。

历史文件保留记录当时的措辞与状态，阅读时先看 [历史索引](history/README.md) 的角色说明，再用现行契约确认今天的行为。
