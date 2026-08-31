# API Pool 项目卡片

## 📋 项目卡片

| 字段 | 值 |
|------|------|
| **领域** | 基础设施 — 多模型 API 端点集中管理 |
| **定位** | 多模型 API 聚合网关，对外统一 OpenAI-compatible 接口 |
| **当前阶段** | 功能维护 |
| **状态** | 🟢 API Pool 2.0 正式运行（唯一实例） |
| **源码** | `/vol1/1000/tool/api-pool2/` (宿主机) |
| **Git remote** | `github.com/849506054/api-pool`（唯一正式分支 `main`） |
| **端口** | 5200 |
| **Python** | 3.13 (宿主机默认) |
| **协议** | OpenAI 兼容 + Anthropic (端点级 protocol 属性) |
| **健康检测** | chat ping / models 探针 (端点级 health_mode) |
| **故障转移** | 普通请求按优先级轮选 → `_try_endpoint` 内部重试 → 明确端点故障才冻结冷却 → 轮转；单请求饿死不冻结、不切换；流式停滞由 API Pool 流事务层处理，不直接冻结端点 |
| **代理** | 端点级 use_proxy 控制 (默认强制直连) |
| **配置持久化** | `api_config.json` (不提交 git) |

## 🎯 里程碑

### 已完成

- [x] **v1.0 基础框架** — HTTP 反代 + 端点管理 + 健康检测
- [x] **手动切换端点** — `/switch_to_endpoint` API + Telegram 通知
- [x] **手动切换修复 (v3)** — `_manual_override_id` 字段与自动路由解耦
- [x] **探活统一为 chat ping** — 消除假阳性
- [x] **thinking `reasoning_content` 修复** — 空串→空格，全量补全
- [x] **瞬态故障冻结保护** — 30s 内 >5 次直接冻结
- [x] **UI 健康状态同步** — `_on_success` 更新 `_health`
- [x] **优先级插入模式** — `update_endpoint` 支持 insert-at-position 重排
- [x] **Anthropic 协议转换** — 消息体/流式/非流式格式转换 (commit d218936)
- [x] **Anthropic 兼容性运行态验收** — 文本、流式、单轮/多轮/并行工具、跨协议工具历史、Hermes 实际工具循环、base64 PNG 图片；验收矩阵：`docs/anthropic-compatibility-matrix.md`
- [x] **协议下拉框恢复** — 从 hidden input 恢复为可见 select (commit 91110b1)
- [x] **test_vision reply 防御** — `_try_endpoint` 返回 string 时 `reply.get()` 防御 (commit afe3326)
- [x] **项目收口** — README / PROJECT.md 定位更新 (commit 31581f2)
- [x] **添加端点代理默认改为强制直连** — HTML 选项顺序 + JS 重置值
- [x] **`extra_payload` 过滤 `response_format`** — 避免 Hermes 插件层参数导致故障转移误判
- [x] **移除瞬态探活，失败直接重试** — 探活消耗 Kcne RPM 配额且通过后真实请求仍失败，改为直接重试一次
- [x] **修复 `tried` 计数器累积导致跳过剩余端点** — 轮转后 `tried=0` 重置，确保所有可用端点都能被尝试
- [x] **移除 `chat()` 层瞬态重试，统一由 `_try_endpoint` 内部 `max_retries` 控制** — 两层重试叠加（4 次/240s/4RPM）改为单层（2 次/120s/2RPM）
- [x] **端点假成功检测** — `check_fake_success` 字段，按端点开关，匹配拒绝内容触发轮转
- [x] **移动端 UI 适配** — 三断点响应式 + 触摸优化
- [x] **Anthropic prompt cache 修复：块级显式 breakpoint** — 顶层 `cache_control` 被 `ps.air-outer.com` 无视，改用 system + 最后消息最后文本块的显式 breakpoint。实测非流式/流式均确认缓存命中（commit 47a2bf3）
- [x] **聚合池自动刷新** — 恢复 5s 间隔状态轮询
- [x] **cooldown_minutes 最低 1** — 不允许 0/负数，保证出错端点走冷却→探活→清除完整恢复流程 (commit 3264380)
- [x] **clear-error 独立解冻 API** — `POST /api/endpoints/:id/clear-error` 只清运行态不改配置，前端按钮同步改造 (commit 3264380)
- [x] **reasoning_text 仅 DeepSeek 模型注入** — 非 DeepSeek 端点跳过，避免无关字段导致兼容性问题 (commit 3264380)
- [x] **auto-strip temperature/top_p** — 端点报 400 且含 temperature/top_p 时自动移除重试 (cherry-pick 上游 6e7531f, commit 8c58aef)
- [x] **tool_call_id_prefix 端点可选项** — Kcne 400 根因修复：tool_call id 格式校验（DeepSeek 官方要求 call_00_ET_ 前缀，跨端点切换混入其他端点格式 id → 400）。Endpoint 新增 tool_call_id_prefix 字段（默认空=不重写），chat() 内确定性重写 id 保配对，Kcne 配 call_00_ET_。17:23 部署真实切换验证通过 (commit 9303572)
- [x] **删除 reasoning_text 注入逻辑** — 8/6+8/7 注入修复经 H/K 测试矩阵证伪（Kcne 校验 tool_call id 与 reasoning 字段无关），注入禁用后 Kcne 46万token 请求正常，正式删除 (commit 4626f16)
- [x] **端点级流式参数配置入口（2026-08-26）** — 编辑/新增端点表单新增「首包超时 / 停滞判定 / 总时长上限」三字段（0=禁用，默认 120/60/120），后端 `_sync_to_config` 白名单与 `_ep_to_dict` 输出补齐，PUT/POST 全链路持久化。触发样本：glm-5.3 长流式请求反复触发 `stream_max_duration` 120s 默认上限被静默截断（近24h 12 次超限）。

### 待办

- [x] **[P0] 2.0 非 DeepSeek Endpoint fallback 兼容** — 已部署至 5200；按目标 Endpoint 隔离 DeepSeek reasoning 字段，真实端点矩阵和 Hermes 5200 链路均已验收。
- [x] **[P3] 提交未 commit 的本地改动** — 已随 9303572/4626f16 提交（含探活竞态去重补丁）
- [x] **[P3] systemd 代理环境收口** — `api-pool2.service` 已内置 HTTPS_PROXY / HTTP_PROXY / NO_PROXY，不再依赖已删除的 1.0 drop-in。
- [ ] **[P2] 待评估：频繁切换端点导致 cache 命中率骤降、增加成本** — 短暂故障应优先原地重试而不是立刻切换，避免丢缓存。可能方案：降权不冻结、首包超时 120s→30~45s、连续 N 次失败才切换 — `proxy.conf` 不在版本控制中，建议文档化或提交模板。（已并入下方 P2 路由与恢复整合评审，见 docs/upstream-borrow-design-v1.md §6）
- [x] **[P3] save_config 原子写（2026-08-27 已部署）** — 改为同目录临时文件 + `flush`/`fsync`/`os.replace`，增加进程内写锁与失败清理；写入失败向上抛出，旧配置文件保持不变。3 项回归测试覆盖完整写入、替换失败保护和并发写入；部署后源码 hash 一致，服务重启正常并加载 27 个端点。设计见 docs/upstream-borrow-design-v1.md 改动三。
- [x] **[P2] 确定性抖动冷却**（2026-08-28 已部署）— `_set_cooldown()` 固定时长乘 80–120% 系数（种子 sha256(ep.id+fail_count)，跨重启可复现）；配额/余额/探活短冷却通道不参与；冷却日志展示抖动后真实时长。8 项单测 + mock 上游 E2E。commit 6561ced，宿主机 hash=`637027c83b3fbbc20d546ce478ce8189`
- [x] **[P2] 客户端类错误分类**（2026-08-28 已部署）— HTTP 400/404/413/422 且无瞬态字样：不冻结、fail_count 不增、不探活、不改路由指针（A′ 轮转不记账）；`chat()` 请求级 `client_error_tried` 集合防不冻结路径死循环；客户端错误跳过候选探活；DEBUG trace 增加 `kind=client_error`。temperature/top_p 与 tool_call_id_prefix 特例不动。12 项单测（含真实 HTTP auto-strip 集成）+ mock 上游 E2E 16/16。commit 6550d88

- [x] **前后端分离** — GUI 从 `GUI_HTML` 常量抽离至 `static/index.html`，服务端 mtime 缓存读取（改文件热更新，前端改动免重启）；删除 GUI_HTML 常量（commit 1996320）
- [x] **日志 flush + 流式超时兜底补洞（2026-08-15）** — `sys_log` 强制 flush、首包 socket 超时受 Endpoint timeout 约束、socket 获取失败与流异常不再静默；已部署并纳入 2.0 运行版本。
- [x] **端点列表卡片自适应** — 卡片高度与右侧列（聚合池管理+聚合链）底部对齐（alignCards），列表内部滚动，5s 自动刷新保持滚动位置；全局滚动条深色细窄风格统一（commit 1996320）
- [x] **端点列表卡片固定 16px 高度差修复（2026-08-15，commit 14370cc）** — alignCards 原用右侧**容器** `getBoundingClientRect().bottom` 作对齐目标，容器 bottom 包含聚合链卡片 `margin-bottom:16px`，导致左侧 epCard 被固定拉高 16px（实测 epCard.bottom=934 vs 聚合链视觉底部=918）。改为取右侧**最后一个卡片**的视觉底部（`right.lastElementChild.getBoundingClientRect().bottom`），实测 diff=0。前端静态文件热更新，无需重启

## 📝 决策日志

| 日期 | 决策 | 理由 |
|------|------|------|
| 2026-06-27 | 单文件 Flask 应用 | 部署简单，su-exec 单进程 |
| 2026-06-27 | systemd 管理 + Restart=always | 原生服务管理 |
| 2026-06-29 | 新增 `_manual_override_id` | v2 修复在 failover 后被自动路由覆盖 |
| 2026-06-29 | 本地改→scp→验证→立即 commit+push | 用户强制要求的工作流 |
| 2026-06-30 | 探活统一为 chat ping | 原 pay_per_use 端点走 `fetch_models` 出现假阳性 |
| 2026-07-01 | thinking 注入空串→空格；全量补全所有 assistant 消息 | 故障转移时 `disable_thinking` 注入空串被 DeepSeek V4 拒绝 |
| 2026-07-01 | 瞬态故障冻结保护：30s 内 >5 次直接冻结 | 探活通过但请求持续失败→原地重试死循环 |
| 2026-07-01 | `_on_success` 更新 `_health = "ok"` | 假阳性探活后 `_health` 残留 bad |
| 2026-07-03 | `update_endpoint` 优先级改为 insert-at-position | 原逻辑会收拢覆盖手动优先级 |
| 2026-07-04 | 定位重新收口：DeepSeek 端点集中管理工具 | 多模型聚合与 Hermes model-name 决策机制冲突 |
| 2026-07-04 | 协议下拉框恢复为可见 select | 被回退为 hidden input，用户发现后恢复 |
| 2026-07-04 | `test_vision` 添加 reply 类型防御（合并上游 fix） | `_try_endpoint` 返回 string 时 `reply.get()` 触发 AttributeError |
| 2026-07-05 | 添加端点代理设置默认改为强制直连 | 新增端点多为本地/直连服务，减少误走代理的提交错误 |
| 2026-07-05 | `extra_payload` 过滤 `response_format` | Hermes 插件层发送 `response_format` 导致故障转移误判，中转链路过滤最安全 |
| 2026-07-05 | tool 消息缺失 tool_calls 时自动补 assistant 回复 | Hermes 插件层消息序列异常导致 Kcne/DeepSeek 400，在 pool.chat() 预处理阶段自动修复 |
| 2026-07-05 | 移除瞬态路径探活，失败直接重试 | 探活请求浪费 Kcne RPM 配额，且 ping 通过后真实请求仍大概率失败 |
| 2026-07-05 | 轮转后 `tried=0` 重置 | `tried` 累积导致循环提前退出，剩余可用端点被跳过 |
| 2026-07-05 | 移除 `chat()` 层瞬态重试，只留 `_try_endpoint` 内部 `max_retries` | 两层重试重叠：内部 2 次 + chat 层 2 次 = 4 次/240s/4RPM，统一为 2 次/120s/2RPM |
| 2026-08-04 | chat() 注入 reasoning_text 替代 reasoning_content | DeepSeek V4 request 字段名是 reasoning_text，reasoning_content 导致 Kcne HTTP 400 |
| 2026-08-04 | _on_success 同时缓存 reasoning_text | 响应含 reasoning_text 时直接缓存，否则从 reasoning_content 映射 |
| 2026-08-04 | 端点假成功检测（`check_fake_success` 字段，默认关闭） | 上游返回 200 OK 但内容含"无法给到相关内容"等拒绝信息，按端点开关触发轮转+冷却 |
| 2026-08-04 | 聚合池卡片自动刷新（5s 间隔） | 之前因性能考虑移除全局定时刷新，导致状态需手动刷新页面 |
| 2026-08-04 | 移动端 UI 适配（768px/480px/380px 三断点） | 手机端操作时布局错乱，按钮溢出，表单无法正常填写 |
| 2026-08-05 | `_on_success` 冷却期间不清除冷却 | 并发请求穿透冷却保护：Ark 429 后冷却被迟到成功请求清除，导致冷却→清冷却→再429 无限循环 |
| 2026-08-07 | cooldown_minutes 最低 1 + clear-error 独立 API | cooldown=0 跳过冷却恢复流程导致错误状态残留；解冻不应改配置值 |
| 2026-08-07 | reasoning_text 注入限定 DeepSeek 模型 | 全局注入会导致非 DeepSeek 端点收到无关字段被拒绝 |
| 2026-08-07 | 上游 Responses API 暂不合并 | 上游 thvse/api-pool 已支持 Responses API (94c1764+9423819)，当前使用场景无关，标记备用 |
| 2026-08-07 | tool_call_id_prefix 端点可选项（默认关闭） | Kcne 400 根因是 tool_call id 格式校验非 reasoning 字段；切换端点时重写 id 为 call_00_ET_ 前缀保配对，其他端点零影响 |
| 2026-08-07 | 删除 reasoning_text 注入逻辑 | 注入/清字段/关 thinking 全部证伪（H/K 测试矩阵），只保留 tool_call_id_prefix 重写；_last_reasoning_text 缓存保留无害 |
| 2026-08-26 | 端点级流式参数显式配置（默认 120/60/120，0=禁用） | glm-5.3 长流式反复触发 120s 总时长默认上限被静默截断；参数属于端点个性差异，应显式配置而非全局默认一刀切 |
| 2026-08-07 | Tokenrhythm 优先级 1，Kcne 降为 2 | 用户指定 Tokenrhythm 为当前使用端点（deepseek-official 未出过 400，不配 prefix）|
| 2026-08-13 | ⚠️ **EXPERIMENTAL 实验版本**（极端情况处理，观察期）四层修改一次性落地 | 08-13 08:00-11:28 大面积端点故障 + Hermes 600s 重试并发 → 33 分钟轮转死循环（详见 skill references/probe-pass-real-request-timeout-loop-2026-08-13.md）。修复：① `_set_cooldown` 幂等化（并发失败不刷新冷却窗口）② chat() 循环顶部冷却跳过（并发请求立即转向）③ 下一级探活失败后并发探活剩余端点（`_check_one_health` 两阶段 11s/21s）④ **prio99 终极兜底**：priority=99 端点正常参与轮换（排最末），全池故障/轮转超 530s 时锁定兜底（60s 容错，530+60=590 < Hermes 600s），成功后续请求 5min 滑动窗口直连，保底失败返回错误走 Hermes fallback（详见 skill references/prio99-fallback-design-2026-08-13.md）|
| 2026-08-13 | deepseek-official priority 5→99（终极兜底端点） | 正常参与轮换（排最末），全池故障/530s 超时锁定兜底。池内优先级：Tokenrhythm=1 / Kcne=2 / kuapi=3 / X5m5x=4 / deepseek-official=99。落盘 api_config.json |
| 2026-08-13 | 冷却恢复探活后台化（commit 5769d32） | `_cleanup_expired_cooldowns` 同步探活 → 后台入队（`_probe_executor` max_workers=3 + `_probe_inflight` 去重）。请求路径（chat/list_endpoints/get_active_chain）不再被冷却过期端点探活阻塞（原实现多端点串行探活每个最长 10s，前端 5s 轮询"轮流上阵"卡顿）。`_background_probe`：通过清冷却+defer 判断+更新 current / 失败续冷 / 异常兜底。defer 延迟切换保 cache 逻辑完整保留（池活跃恢复端点延迟 5min）。设计确认：后台探活与真实请求并发无害，inflight 只防重复探活不锁真实请求。5 场景烟测 + 重启 active |
| 2026-08-22 | 单请求饿死处理边界修正 | `_last_success_ts` 在端点 timeout 窗口内命中时，判定为单请求饿死：不冻结、不改 `_current_endpoint_id`、不清手动覆盖、不探活、不切换端点；本次失败交回 Hermes 现有重试机制。（取代 08-14 旧记录） |
| 2026-08-22 | 流式停滞与端点冻结解耦并部署 | 下游流式停滞不等同端点故障；`_timeout_abort` 不再冻结端点。尚未向下游输出有效内容时，API Pool 对同一端点内部重试一次；已有输出时不做透明续传，避免重复内容。错误 SSE 改用 `json.dumps`，修复 `Unterminated string`。服务 `api-pool2.service` 已重启，工作区/宿主机 hash=`896b52042c85c9e02834582281c11afd`，测试 `28 tests, OK`。 |
| 2026-08-22 | **Anthropic 缓存修复：顶层 cache_control → 块级显式 breakpoint** | `ps.air-outer.com` 网关无视顶层缓存字段，只认消息块级显式 `cache_control`。2026-08-21 旧结论（326 token 小前缀假阴性）已更正。同时补全流式 usage chunk 的 `prompt_tokens_details`。提交：`47a2bf3` |
| 2026-08-21 | API Pool 1.0 生命周期终局 | 1.0 服务、目录、备份与封存分支全部删除；2.0 成为唯一正式实例，`main` 成为唯一正式分支。 |
| 2026-08-28 | quota_markers 补齐 AgentRouter 402 文案（commit 8751b32，已部署） | AgentRouter "Budget pool quota has been exhausted" 402 不匹配既有 quota 词典（差 "has been"），只走 5 分钟短冷却；UI 5s 轮询触发冷却过期探活 → 每 5 分钟刷两条 WARN。补一个 marker 后按 quota_exceeded 默认 5h 冷却。测试 78 OK；宿主机 hash=`13a1dd00a681c98247fc598dc8d08195`。 |
| 2026-08-29 | UI 聚合池优先级实时刷新修复（commit 4d4fef0） | 焦点守卫（防 5s 自动刷新收回下拉框）无法区分「下拉展开中」与「选择已完成」——change 触发后 `<select>` 仍持焦点，`setPriority()` 末尾的主动 refresh 与后续 5s 轮询全部被守卫拦截，卡片一直显示旧顺序。修复：优先级下拉框 onchange 末尾追加 `this.blur()`，选择完成即释放焦点、立即重绘；下拉展开期间不触发 change，原防收回功能不受影响。纯前端静态文件 mtime 热更新，无需重启服务。 |
| 2026-08-29 | Git 历史清洗：移除误推的已否决提交 | 工作区 ahead 2 时直接 push，把一条用户已否决的提交连带推上远端。处理：rebase 重放保留有效修复（哈希 441c3f6→4d4fef0）+ force-with-lease 推送 + reflog expire/gc 清理本地残留，全历史验证特征串零残留；同步清理引用旧哈希的文档记录。沉淀为「推送前检查 ahead N」流程。 |
| 2026-08-30 | 分组路由（priority_by_group 分组隔离）实施部署 + UI 优先级接线修复 | 分组池实施已部署生产，bg 组真实流量（background_review / knowledge-sync cron）验证通过。诊断发现 UI 下拉框从未接线到分组接口：仍走旧全局 `priority` PUT 路径，而路由只读 `priority_by_group`——全局改动零效果且被 `_renumber` 静默回滚。修复：`setPriority()` 改为带组参数 `POST /api/priority/<id>?group=&priority=`。遗留：编辑表单 saveEndpoint() 的「优先级」字段仍走全局 PUT，被遮蔽未修。 |
| 2026-08-30 | 越权组筛选功能回滚删除 | 端点列表的 `🏷️组名` 筛选标签（groupCounts/isGroupFilter/renderEndpoint 组标签）不在 8/29 批准终态清单范围内（组筛选标签栏仅授权于聚合池/聚合链两处），用户追问后 3 处全部删除并部署。沉淀「UI 实现范围铁律」：清单外功能须先请示或显式标注，不得默认保留。 |
| 2026-08-30 | 移除聚合池「⬆️ 按优先级」按钮及功能链路，原位预留「➕ 新建分组」按钮 | 用户决策：分组池上线后手动按优先级重置入口已无必要。同步移出前端 `resetPriority()`、后端 `POST /api/reset-priority` 路由与 `reset_to_priority_mode()` 方法（互为唯一调用方，无测试引用）；原按钮位置新增 `➕ 新建分组` 预留位（`createPoolGroup()` 仅 toast 提示暂未开放，沿用 ⚙️ 编辑分组占位惯例）。117 测试全绿 + mock E2E（旧路由 404）+ 5200 生产验证。 |
| 2026-08-30 | 聚合池卡片跨组端点标识简化：跨组当前端点紫色 badge 由组名标签形态改为 `● 组名` | 用户决策：去图标与「当前」文字，用 ● 前缀+空格区分其他组当前端点。仅聚合池成员卡片；聚合链标识不变。前端热更新直接部署（无需重启）。 |
| 2026-08-30 | 分组实体（mixed/dedicated + model 选择器）部署生产 | 组实体 `pool_group_defs`：{name, type: mixed\|dedicated, model}；main 恒存（selector 固定 api-pool）。路由解析升级：请求 model 先匹配组选择器→再匹配组名→未命中回 main，现网路由零变化。REST `GET/POST /api/groups`、`PUT/DELETE /api/groups/<name>`；dedicated 组入组模型校验（不匹配自动过滤/端点改模型自动移出）；删组成员逐个移出+指针/计数清理；改名同步端点 pool_groups、三个指针态、fallback 计数。前端新建/编辑分组弹窗（main 锁定禁用）。133 测试全绿 + 本地 mock E2E 后部署：宿主机 hash 后端 `5ce04097` / 前端 `3a38ed86`，服务重启正常，`/api/groups` 返回 main(6 成员)+api-pool-bg(4 成员)，`/v1/models`=`['api-pool','api-pool-bg']`（main 条目消失=预期语义变化，selector 列表取代组名列表）。旧配置零迁移（无 defs 时从端点声明派生，首次编辑才落盘）。设计详情见 skill references/pool-group-implementation-status-2026-08-30.md。 |
| 2026-08-30 | 主子组单向互斥修复 | 明确分组占用契约：main 不受子组粘性或在途状态约束；子组仅避让 main 当前/手动/在途端点。main 接管共享端点后，子组从剩余健康候选中按本组优先级从 1 重新选择，不做环形顺延；无候选时沿用子组 fallback 到 main。`_inflight_owner` 改为端点→组→计数，防止 main 抢占时覆盖子组 owner 或同组并发提前释放。新增单向互斥、优先级重选及并发计数回归测试；全套 135 测试通过。 |
| 2026-08-30 | 零成员新分组立即可见修复 | 新建组已成功写入 `pool_group_defs`，但 `_all_group_names()` 只枚举端点归属与路由指针，导致 `/api/groups`、`/api/chain` 漏掉零成员组；聚合池标签和端点「入池」弹层也无法显示。修复为组名全集包含 `_group_defs`；聚合池标签与入池弹层以 `/api/groups` 实体列表为主、运行态摘要为补充。新增零成员组 API 回归测试；全套 136 测试通过。 |
| 2026-08-30 | 聚合池按站点选模与端点列表站点分类 | 端点新增显式 `site_name`；端点列表移除启用/禁用/入池/视觉/计费/模型厂商筛选，只保留动态「全部 + 站点」标签。聚合池模型名可点击并读取该端点 `/models`（前端缓存 5 分钟、支持搜索）；选模事务在当前组内以同站点目标模型端点替换源端点：已有配置则复用，否则仅复制连接/行为配置并生成 `站点-模型` 名称；继承当前组优先级与路由指针，源端点配置及其他组归属不变，专用组模型约束继续生效。新增站点持久化、模型读取、复用/克隆、跨组隔离、优先级/指针迁移、名称冲突和 API 路由测试；全套 146 测试通过。 |
| 2026-08-30 | `site_id` 缓存账户硬隔离 | 缓存账户不再由运行时临时比较 Key，而由持久化 `site_id` 标识：规范化 base_url + API Key 相同的端点自动复用同一 `site_id`，任一不同则生成不同 ID；`site_name` 仅用于 UI 分类。克隆同账户模型端点继承 `site_id`；端点连接信息变化时自动重新解析。每组最近成功 usage 的 `site_id` 变化，或同请求故障轮转到不同 `site_id` 时，目标账户首条本地缓存统计记 0；客户端上游 usage 不修改，后续同账户请求正常统计。新增 3 项边界测试，全套 149 测试通过。 |
| 2026-08-30 | 池内端点日志统一格式 | 新增统一标签 `[池名]端点名[: 模型名]`。请求入口日志改为 `收到 API 请求，尝试请求端点 '[api-pool-bg]Opencode: deepseek-v4-flash'`；成功、失败、冷却、候选探活、流式异常、图片解析、手动切换及恢复日志统一在端点名前带所属池。移除请求成功日志中无条件写死且未经过延迟阈值判断的 `(延迟: 正常)`；真实耗时继续由对话日志 `latency_ms` 记录。新增 2 项格式测试，全套 151 测试通过。 |
| 2026-08-30 | 端点编辑窗口移除启用/入池/路由组 | 端点编辑表单删除「启用」「加入聚合池」「路由组」三个配置项；新增端点默认 `enabled=true`、`in_pool=false`、`pool_groups=[]`，编辑已有端点时不再提交这三项、保留其现状。后端未入池端点默认无池组，入池时绑定所选组，出池/移出最后一组时清空组绑定；旧已入池配置缺 `pool_groups` 字段时兼容为 main。全套 151 测试通过。 |
| 2026-08-31 | 聚合池原生模型下拉与切换诊断收口 | 页面刷新时利用 5 分钟缓存预取池内端点模型目录，并在原生 select 的 `mousedown`/`focus` 双路径确保选项就绪，适配 iOS 首次打开快照；模型切换接口补充成功、参数错误、目标不存在和约束冲突日志，前端失败提示包含分组与目标模型。新增后端日志及前端接线回归测试；全套 153 测试通过。 |
| 2026-08-31 | P1 聚合池分组操作验收矩阵 | 补齐组 REST 新建/改名/删除及每步配置落盘、追加式跨组入池、组感知移出及最后一组整体出池、分组定义+组内优先级+当前端点联合重启恢复、前端单击组名追加式入池契约。只新增测试与项目记录，不改生产功能；全套 157 测试通过。 |
| 2026-08-31 | P1 分组路由 REST 验收矩阵 | 补齐按组手动切换和组内优先级两个 REST 联合场景：手动切换仅更新目标组 current/manual/runtime_state 并保留 main；优先级调整仅重排目标组、持久化 `priority_by_group`，下一次该组请求实际命中新 #1。只新增测试与项目记录，不改路由实现；全套 159 测试通过。 |
| 2026-08-31 | 路由决策版本保护 | 每组维护路由 epoch；请求开始后发生手动切换、恢复回迁或其他有效轮转时，迟到的旧请求仍可完成并更新端点健康状态，但不再覆盖较新的 current/manual 或持久化指针。恢复探活仅在真正更新指针后记录“立即回切”，手动端点阻止回迁时明确记录保留手动端点。新增迟到成功、迟到失败、真实在途并发和恢复回迁回归测试；全套 163 测试通过。 |
| 2026-08-31 | 分级重试与流式超时语义收口 | 可恢复的 5xx、连接/超时、流式首条数据超时和假成功在原端点额外重试，首轮退避 3 秒并受 530 秒整体预算约束；近期有成功响应的超时在重放前判定单请求饿死。纯 429 优先采用 Retry-After。普通连接/响应头超时与流式首条数据超时解耦；流式总时长默认 0（不限），显式限制触发时向下游标记 error 和可见截断原因。请求入口、重试、成功和失败日志共享短 request ID。编辑 UI 同步展示真实字段语义。全套 169 测试通过。 |
| 2026-08-31 | 组名校验允许点号 | 组名含 `.` 创建失败（如 `gpt-5.6-sol`）；`_valid_group_name()` 正则 `[\w\u4e00-\u9fff-]+` → `[\w\u4e00-\u9fff.-]+`，create/update 组名非法错误提示同步更新；新增点号组名单测。已部署生产。组名 ≠ model 选择器（生产 = 组 `api-pool-gpt` + 选择器 `gpt-5.6-sol`）。 |

## 📌 活跃事项

### 下一阶段进度（2026-08-31）

- [x] **P0 当前生产增量收口** — 原生模型下拉预取与切换诊断已固化，153 测试通过，commit `163265f`。
- [x] **P1 聚合池分组操作验收** — REST CRUD/落盘、追加式跨组入池、组感知移出、联合重启恢复和前端交互契约已覆盖，157 测试通过，commit `07b7fc1`。
- [x] **P1 分组路由 REST 验收** — 按组手动切换和组内优先级联合场景已覆盖，完整测试 159 项通过，commit `e443cc2`。
- [x] **developer 角色不兼容 400 同请求降级（2026-08-31 已部署）** — Hermes 对 gpt-5*/codex 会话把首条 system 换成 developer 角色；端点级探测证实「不认 developer」是端点/上游级差异（DS 的 AgentRouter-ds4f、qnaigc 拒绝，GLM 家族分裂：Tokenrhythm 接受 / Opencode-glm5.3f 拒绝，siliconflow-vision 拒绝，Kcne/Stepfun 接受）。客户端类错误轮转路径识别角色拒绝签名（unknown variant `developer` / Input tag 'developer' / Incorrect role information / role错误）后，同请求内把 developer 降级为 system 重试后续候选，内容不变。8 项新增测试 + 全量 178 项通过；生产 md5 `1a45f00a5a09aa384f1e66c7925d66c8`。commit `75c70c2`。
- [x] **[P1] 子组→main 组级延迟回切（2026-08-31 已部署）** — `_group_fallback_lock_until` + `_GROUP_FALLBACK_RETURN_SECONDS=300`：入口/耗尽 fallback 建锁；锁定期内该子组请求直走 main 并滑动顺延窗口（无请求 5 分钟才回组试探，成功回组粘性，失败重新锁）；组改名/删除迁移锁。6 项新增测试 + 全量 184 项通过；生产 md5 `375ea30c1f1748830e25b3785b099803`，真实流量验证 `fallback 锁定中（剩余 274s），本请求走 main 组`、入口 fallback WARN 归零。commit `c241b72`。
- [x] **P0 生产真实流量对账（2026-08-31 完成）** — 按 PID 分世代对账（req 标签 13:29 上线；13:29+ 新世代 189 请求）：req ID 贯穿 0 缺口；原端点重试全部 1/1 次+3s 退避；同请求同端点重复 ERROR=0；单请求饿死真实样本正确处置；新世代总时长截断 0 起、60s 停滞 3 起全部不冻结且端点随后成功；A0 部署前入口 fallback 风暴 107 次 → 部署后归零。缺口仅余 429/Retry-After 无样本（留 C 项）。详见 api-pool-management `references/next-phase-progress-2026-08-31.md` P0 收口节。
- [x] **P1 有效业务增量停滞检测（2026-08-31 已部署）** — readline 循环体内 `business_stall_deadline`：仅 content/reasoning/tool 增量与结束事件（finish_reason/usage/Anthropic content_block_*/message_delta/message_stop）刷新时钟，SSE 心跳注释/空行/空 delta 不再给坏死流续命；超时复用 `_timeout_abort`（无输出原端点重试一次，已有输出不重放不冻结）；复用 `stream_stall_timeout` 数值，0=禁用；OpenAI 透传与 Anthropic 转换双分支接线。顺手修复无输出中止分支字面 `\n\n` 反斜杠缺陷。4 项新增测试 + 全量 188 项通过；生产 md5 `09981c83b1fb832a2b3398ef1eb5fd42`。commit `ed1b181`。
- [x] **B2 流式生命周期日志贯穿 request ID（2026-08-31 已部署）** — 流式首包超时、socket 兜底、停滞/总时长超限/Anthropic 提前结束、内部重试失败、客户端断开与流内异常统一补 `[req=]`；不改变流式处置、重试、冻结或路由语义。新增生命周期日志回归测试，全量 189 项通过；生产 md5 `ad5a2bd0c291b9e123d76f5b505d1409`。commit `05b5117`。
- [ ] **P1 Hermes 流式错误结束 E2E** — 本地 mock 验证显式总时长触发后的 `finish_reason:error`、可见截断原因、不重复输出与不冻结端点。
- [ ] **P1 重试边界收口** — 后端归一化 `max_retries=0..3`；逐项核对图片转译、视觉解析、流式内部重试、temperature/top_p 递归和 bg→main fallback 对 request ID、剩余预算与重试上限的继承。
- [ ] **P1 分组 UI 一致性验收** — PC 1440px 保持 7:3 双列、移动端 390px 单列且无横向溢出已由生产 CDP 验证；待 9222 浏览器恢复后刷新页面，复核聚合池 main 计数 7 与 `/api/groups`/`/api/chain` 计数 6、当前端点显示差异。结案前不修改生产代码，不人为制造 deferred 样本。详细证据与统一下一步见 api-pool-management `references/next-phase-progress-2026-08-31.md`。

### 工作区归属

`workspace/` 是本地工作区整理产物，不属于发布源码树，已由 `.gitignore` 排除。其内容按来源处理：

- `workspace/repositories/github-sync/` 保留 API Pool 正式 Git 历史，用于只读核对和同步参考；
- `workspace/repositories/github-verify/` 是验证用浅克隆，不作为开发入口；
- `workspace/experiments/`、`workspace/snapshots/`、`workspace/records/`、`workspace/tools/`、`workspace/archive/` 是实验、部署快照、记录、工具和历史归档，不进入 GitHub 发布树；
- 正式开发目录是本项目根目录，分支跟踪 GitHub `main`。

这些目录不删除；需要恢复历史时，应从对应独立 clone 或 `/opt/data/backups/` 读取，不应将整个 `workspace/` 作为新代码提交。

- [x] **[P0] API Pool 2.0 正式运行** — 独立目录 `/vol1/1000/tool/api-pool2`、unit `api-pool2.service`、端口 5200；Hermes 主路由已切换并验收。
- [x] **[P0] 非 DeepSeek Endpoint fallback 兼容** — 已完成真实端点矩阵、协议转换、工具调用和 Hermes E2E 验收。
- [x] **[P0] 2.0 多模型上游建模** — **不属于当前 2.0 范围，已否决**；不引入 ModelRoute/独立 Upstream，继续一端点一模型。
- [x] **[P1] claude-opus-5 直连失败归纳** — **不扩展为当前 2.0 的通用模型路由/协议适配任务**；只作为非 DeepSeek Endpoint 真实矩阵的一个验证目标。
- [x] **[P0] 统一 reasoning 意图适配** — **不属于当前 2.0 范围，已否决**；本版本只隔离 DeepSeek 专属字段，不做 reasoning wire 统一转换。

- [x] **[P1] 端点自定义 User-Agent（2026-08-17 已部署）** — 端点配置新增 `default_headers`，UI 提供可选 User-Agent；聊天、Models 探活、获取模型、延迟/多模态测试统一应用。实测 `ps.air-outer.com` 默认 UA 返回 401，`hermes-agent/0.20.1` 返回 200 并获取 3 个模型。部署后 16 端点全部带 `default_headers` 字段（存量端点 `{}` 向后兼容）；service 重启后新进程无 ERROR。

### 当前阶段：生产请求可靠性与故障判断

当前目标是确保 API Pool 在真实生产请求中的路由、故障判断和响应事务行为正确、稳定、可解释。当前阶段不是性能评测、容量评估或吞吐优化项目。

#### P0：生产请求可靠性

- [x] **路由与恢复语义统一** — 已部署并进入观察。明确区分当前端点粘性、缓存保护与延迟回切、故障冷却和终极兜底锁定；`deferrable` 底层字段兼容保留，产品语义为当前端点缓存保护；自动恢复不覆盖手动指定端点；延迟状态解除后路由指针与实际回切结果一致。
- [x] **探活结果真实性** — 已部署并进入观察。成功但较慢的探活保持 `slow`；未在观察窗口内完成的探活不直接判定为失败；只有明确 `bad` 结果才触发短冷却；后台探活、全量健康检查和请求故障转移遵守端点级去重。
- [x] **流式事务判定正确性** — 已部署并进入观察。明确区分首包超时、连续无新数据停滞、流式总时长超限、Anthropic 提前结束、上游异常和客户端断开；流式事务失败不直接冻结端点；未产生有效输出时允许同端点内部重试一次，已有输出后不透明重放。
- [x] **复核错误分类与端点处置矩阵** — 已定稿 `docs/error-handling-matrix.md`：19 类错误 × 重试/冷却/探活/切换处置，基于 commit `d5d1f6d` 代码路径与 2026-08-25 部署后真实样本（429/500/503/超时/流式停滞/总时长超限/探活恢复/defer 回切）交叉验证；确认请求级异常不升级为端点级故障。遗留观察项：手动切换无日志（小改待授权）、`_transient_count` 死代码清理（独立小项）。
- [x] **请求控制路径可解释性** — DEBUG 请求级诊断已完成。DEBUG 关闭时不增加额外 payload 遍历、网络请求、探针、重试、线程或路由动作；DEBUG 开启时记录端点尝试、内部重试、端点切换和最终状态，不记录完整敏感请求内容，也不逐 chunk 记录流式响应。

#### P1：真实问题驱动的复核

- [x] **观察本轮三个修复的实际行为** — 已结案（2026-08-28）。48h 观察窗口干净：手动切换正确清 defer 并保留冷却状态；AgentRouter 402 配额词典修复（commit 8751b32）经生产真实触发验证——402 正确分类为 `quota_exceeded`，冻结 18000s（默认 5h，响应无 retry-after），不再出现短冷却循环；流式停滞 3 次均正确判「流式事务失败，不冻结端点」，无误冻结样本。
- [x] **复核故障转移控制路径** — 已结案（2026-08-28）。48h 窗口内无 failover 控制路径异常样本，未触发复核条件，按真实问题驱动边界保持现状；实测一次手动指定端点 → 402 → 配额冻结 → 候选探活 → Opencode 兜底成功的完整链路，行为符合设计。（组合行为矩阵见 P2 整合评审 §6，「不加复杂路由状态机」仍为硬边界）
- [x] **按真实故障触发复核对话日志管线** — 已结案（2026-08-28）。48h 窗口内无日志写入阻塞、SQLite 锁冲突、后台线程积压或存储异常证据，按边界不改动。

#### 明确移出当前阶段

- [x] **移出性能评测主线** — 当前阶段不开展 payload 规模基准、P50/P95/P99、Hermes 直连对照、固定开销拆分、吞吐/容量评估，也不为这些评测增加常驻计时或网络请求；未来如确有需要，另建独立性能评估阶段。

**阶段边界**：当前阶段只处理 API Pool 生产请求的路由正确性、故障判断、冷却与恢复、缓存保护、fallback 和流式事务完整性。工作由真实故障样本驱动，优先最小确定性修复。DEBUG 仅用于解释已有请求控制路径，不承担性能基准。除非出现明确生产证据，本阶段不进行 payload 基准、吞吐/容量评估、日志管线重构、路由状态机扩张或多模型上游建模。
