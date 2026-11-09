# 历史与版本追溯

现行阶段见 [PROJECT](../../PROJECT.md)，当前设计见 [契约索引](../contracts/README.md)。本目录原文保留当时措辞，身份与适用时期由本页说明。

## 日期与主题

| 日期 | 采用结果与证据路线 |
|---|---|
| 2026-08-18 | 最小非 DeepSeek 故障转移、协议兼容与入口过滤进入初版；[阶段总结](phases/phase-summary-2026-08-18.md) 是当时实验阶段记录 |
| 2026-08–09 | 原子配置写、冷却抖动、失败分类及并发写回逐步采用；旧矩阵作为当时已验证行为记录，当前分类以 [错误契约](../contracts/endpoint-error-classification.md) 为准 |
| 2026-09-06 | 手动切换采用用户断言重置观测态；2026-10-30 收口为单次指针与本组等待作废，[现行契约](../contracts/manual-switch-cooldown-noop-2026-09-06.md) |
| 2026-09-13 | Gemini 第四上游协议已部署并真实验收，工具续轮带 thoughtSignature；[设计](plans/gemini-protocol-bridge-plan-2026-09-12.md) / [实施分解](plans/2026-09-12-gemini-protocol-bridge-implementation.md) |
| 2026-09-18–10-30 | 空闲借用、按组 defer 与 main 时间戳判据演进，最终按本组真实交接释放；[现行借用契约](../contracts/idle-borrow-contract-2026-09-18.md) |
| 2026-09-27–11-07 | 子组引用从 main 泛化到任意普通组；父轴名次、共享指针并发与故障回切采用名次语义，[现行契约](../contracts/subgroup-join-main-contract.md) |
| 2026-10-05–11-03 | 主动轮换采用成功次数与参与资格检查；后备成员保持各自故障路由身份，[轮换契约](../contracts/group-rotation-contract-2026-10-05.md) |
| 2026-10-20–11-04 | 配置字段改数据类推导；预算未出站即收尾与已出站失败记账分开，退避全通道 24 秒封顶；[字段](../contracts/endpoint-field-addition-checklist.md) / [重试](../contracts/rate-limit-retry-switch-2026-09-20.md) |
| 2026-11-02–03 | 身份透传/同步与自主出站静默上线；图片解析缓存保留历史并接回统一链；[身份](../contracts/client-identity-and-outbound-policy.md) / [视觉](../contracts/vision-pool-group-implemented-2026-09-10.md) |
| 2026-11-05 | 失败探活移到池锁外、额度耗尽进入 24 小时冷却；额度自然触发证据归 [观察缺口](../../PROJECT.md) |
| 2026-11-07 | 日志 trim 扫描移到写锁外、分批更新、入队时取事件时间；隔离与部署后真实落库已核，当日完整自然清理周期（7 天保留清理与每小时截断）无报错 |
| 2026-11-08 | 凭据出站副本脱敏上线；页面 gzip/ETag 与管理 JSON 压缩部署并获用户验收。页面正文 207,319 → 56,749 字节，状态三接口样本 117,288 → 11,498，未变页面 304 零正文；[传输契约](../contracts/ui-http-transfer.md) |
| 2026-11-09 | 项目资料按现行正文、历史、本机操作与本地证据分区，整理基线 `main` / `c95dad8` |

## 初版资料归档角色

| 原文件 basename | 角色与适用范围 |
|---|---|
| [anthropic-compatibility-matrix.md](compatibility/anthropic-compatibility-matrix.md) | 采纳后的阶段验收记录，矩阵为当时覆盖；现行入口为架构与测试 |
| [error-handling-matrix.md](compatibility/error-handling-matrix.md) | 被替代的错误处置矩阵；现行分类含 11 月预算/客户端冻结决定 |
| [cross-model-routing-fallback-design-v2.md](designs/cross-model-routing-fallback-design-v2.md) | 被替代的 ModelRoute 设计；现行使用一端点一模型和组路由 |
| [multimodel-upstream-design-v0.md](designs/multimodel-upstream-design-v0.md) | 被替代的早期多模型上游设计 |
| [minimal-non-deepseek-fallback-design-v1.md](designs/minimal-non-deepseek-fallback-design-v1.md) | 已采纳的初版最小故障转移设计；后续协议与模型兼容已演进 |
| [tier-scheduler-design.md](designs/tier-scheduler-design.md) | 历史愿景；复杂度主动选模与成本调度是讨论项 |
| [content-filter-design-v1.md](designs/content-filter-design-v1.md) | 已采纳的入口过滤初版；当时因果待核，后来已有隔离 A/B 与零宽规则 |
| [upstream-borrow-design-v1.md](designs/upstream-borrow-design-v1.md) | 分项采纳、部分被替代的评审稿；原子写/抖动已采用，客户端类处置以 11 月冻结契约为准 |
| [phase-summary-2026-08-18.md](phases/phase-summary-2026-08-18.md) | 已采纳的阶段总结；实验实例定位与待办只表示当时状态 |
| [gemini-protocol-bridge-plan-2026-09-12.md](plans/gemini-protocol-bridge-plan-2026-09-12.md) | 已采纳并于 2026-09-13 部署的 Gemini 设计；待批措辞属原稿时期 |
| [2026-09-12-gemini-protocol-bridge-implementation.md](plans/2026-09-12-gemini-protocol-bridge-implementation.md) | 已执行的 Gemini 初版实施分解；生产于 2026-09-13 上线 |

## 本地交付证据

Gemini 2026-09-12 隔离实线与 2026-09-13 部署的完整一次记录归本地 `workspace/archive/documentation-pilot-20261109/repo-legacy/docs/evidence-20260912-gemini-e2e/` 与 `evidence-20260913-t7-deploy/`。公开历史保留采用结果，完整原文按原字节追溯；公开旧稿的环境路径使用可移植占位符。

## 完整本地原文

原项目卡片及全部长台账按原字节保存在项目根相对位置 `workspace/archive/documentation-pilot-20261109/repo-legacy/PROJECT.md`，外层身份索引为同目录 `INDEX.md`。该记录包含当时环境、部署与私有样本，仅供本地追溯。

冻结技能源正文在项目根相对位置 `workspace/archive/documentation-pilot-20261109/skill-legacy/references/`；现行设计抽取到 `docs/contracts/`，完整原文里的事故、部署过程与本机操作留在本地来源档案。迁移逐文件映射、SHA-256 与检查读数在 `workspace/records/documentation-pilot-20261109/repo-migration.json`。

## 旧路径映射

早期原文里的旧路径按 basename 在上文角色表定位；原 docs 下的 Gemini 两稿现归 `history/plans/`，两份矩阵归 `history/compatibility/`，阶段总结归 `history/phases/`，初版设计归 `history/designs/`。原 `docs/evidence-20260912-gemini-e2e/` 和 `docs/evidence-20260913-t7-deploy/` 现归项目本地 `workspace/archive/documentation-pilot-20261109/repo-legacy/docs/` 下同名目录。历史正文的当时路径只作原始定位，现址以上述映射为准。

## Git 追溯

在仓库根按旧路径查看最后一版和演进：

```bash
git show c95dad8:PROJECT.md
git log --follow -- docs/anthropic-compatibility-matrix.md
git show c95dad8:docs/gemini-protocol-bridge-plan-2026-09-12.md
git log --follow -- docs/history/plans/gemini-protocol-bridge-plan-2026-09-12.md
```

`c95dad8` 是本次整理前锚点；迁移落入 Git 后可用新路径继续追踪。Git 命令在本次整理中作为用户查找路线写入，仓库版本状态由提交负责人核验。
