# 现行契约

本目录是 API Pool 现行代码契约正文。文件名保留来源 basename 以便维护技能稳定引用，标题与正文使用当前行为。迁移来源是 2026-11-09 冻结的维护技能引用文件；具体映射和保留方式见本地迁移清单。

| 主题 | 正文 |
|---|---|
| 错误、冻结、恢复与流事务 | [端点错误分类](endpoint-error-classification.md) |
| 普通组引用专用子组、父轴名次与恢复 | [子组引用](subgroup-join-main-contract.md) |
| 成功次数触发与参与集合 | [组内轮换](group-rotation-contract-2026-10-05.md) |
| 限流开关、首包分类与退避 | [重试](rate-limit-retry-switch-2026-09-20.md) |
| 用户指定、等待作废与组归属 | [手动切换](manual-switch-cooldown-noop-2026-09-06.md) |
| 图片解析、历史与缓存 | [视觉池](vision-pool-group-implemented-2026-09-10.md) |
| 透传、伪装、同步与池自身出站 | [身份](client-identity-and-outbound-policy.md) |
| 出站副本、识别与失败处置 | [凭据脱敏](outbound-credential-redaction-2026-11-08.md) |
| 页面压缩、条件 GET 与实时 JSON | [HTTP 传输](ui-http-transfer.md) |
| 数据类、接口、表单与持久化 | [字段扩展](endpoint-field-addition-checklist.md) |
| main 空闲窗口、避让与延迟释放 | [空闲借用](idle-borrow-contract-2026-09-18.md) |

测试命令与证据层次见 [testing](../testing.md)。部署事件和历史版本见 [history](../history/README.md)，环境与本机操作见维护技能 `api-pool-management`。
