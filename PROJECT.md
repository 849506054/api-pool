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

### 待办

- [x] **[P0] 2.0 非 DeepSeek Endpoint fallback 兼容** — 已部署至 5200；按目标 Endpoint 隔离 DeepSeek reasoning 字段，真实端点矩阵和 Hermes 5200 链路均已验收。
- [x] **[P3] 提交未 commit 的本地改动** — 已随 9303572/4626f16 提交（含探活竞态去重补丁）
- [x] **[P3] systemd 代理环境收口** — `api-pool2.service` 已内置 HTTPS_PROXY / HTTP_PROXY / NO_PROXY，不再依赖已删除的 1.0 drop-in。
- [ ] **[P2] 待评估：频繁切换端点导致 cache 命中率骤降、增加成本** — 短暂故障应优先原地重试而不是立刻切换，避免丢缓存。可能方案：降权不冻结、首包超时 120s→30~45s、连续 N 次失败才切换 — `proxy.conf` 不在版本控制中，建议文档化或提交模板

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
| 2026-08-07 | Tokenrhythm 优先级 1，Kcne 降为 2 | 用户指定 Tokenrhythm 为当前使用端点（deepseek-official 未出过 400，不配 prefix）|
| 2026-08-13 | ⚠️ **EXPERIMENTAL 实验版本**（极端情况处理，观察期）四层修改一次性落地 | 08-13 08:00-11:28 大面积端点故障 + Hermes 600s 重试并发 → 33 分钟轮转死循环（详见 skill references/probe-pass-real-request-timeout-loop-2026-08-13.md）。修复：① `_set_cooldown` 幂等化（并发失败不刷新冷却窗口）② chat() 循环顶部冷却跳过（并发请求立即转向）③ 下一级探活失败后并发探活剩余端点（`_check_one_health` 两阶段 11s/21s）④ **prio99 终极兜底**：priority=99 端点正常参与轮换（排最末），全池故障/轮转超 530s 时锁定兜底（60s 容错，530+60=590 < Hermes 600s），成功后续请求 5min 滑动窗口直连，保底失败返回错误走 Hermes fallback（详见 skill references/prio99-fallback-design-2026-08-13.md）|
| 2026-08-13 | deepseek-official priority 5→99（终极兜底端点） | 正常参与轮换（排最末），全池故障/530s 超时锁定兜底。池内优先级：Tokenrhythm=1 / Kcne=2 / kuapi=3 / X5m5x=4 / deepseek-official=99。落盘 api_config.json |
| 2026-08-13 | 冷却恢复探活后台化（commit 5769d32） | `_cleanup_expired_cooldowns` 同步探活 → 后台入队（`_probe_executor` max_workers=3 + `_probe_inflight` 去重）。请求路径（chat/list_endpoints/get_active_chain）不再被冷却过期端点探活阻塞（原实现多端点串行探活每个最长 10s，前端 5s 轮询"轮流上阵"卡顿）。`_background_probe`：通过清冷却+defer 判断+更新 current / 失败续冷 / 异常兜底。defer 延迟切换保 cache 逻辑完整保留（池活跃恢复端点延迟 5min）。设计确认：后台探活与真实请求并发无害，inflight 只防重复探活不锁真实请求。5 场景烟测 + 重启 active |
| 2026-08-22 | 单请求饿死处理边界修正 | `_last_success_ts` 在端点 timeout 窗口内命中时，判定为单请求饿死：不冻结、不改 `_current_endpoint_id`、不清手动覆盖、不探活、不切换端点；本次失败交回 Hermes 现有重试机制。（取代 08-14 旧记录） |
| 2026-08-22 | 流式停滞与端点冻结解耦并部署 | 下游流式停滞不等同端点故障；`_timeout_abort` 不再冻结端点。尚未向下游输出有效内容时，API Pool 对同一端点内部重试一次；已有输出时不做透明续传，避免重复内容。错误 SSE 改用 `json.dumps`，修复 `Unterminated string`。服务 `api-pool2.service` 已重启，工作区/宿主机 hash=`896b52042c85c9e02834582281c11afd`，测试 `28 tests, OK`。 |
| 2026-08-22 | **Anthropic 缓存修复：顶层 cache_control → 块级显式 breakpoint** | `ps.air-outer.com` 网关无视顶层缓存字段，只认消息块级显式 `cache_control`。2026-08-21 旧结论（326 token 小前缀假阴性）已更正。同时补全流式 usage chunk 的 `prompt_tokens_details`。提交：`47a2bf3` |
| 2026-08-21 | API Pool 1.0 生命周期终局 | 1.0 服务、目录、备份与封存分支全部删除；2.0 成为唯一正式实例，`main` 成为唯一正式分支。 |

## 📌 活跃事项

- [x] **[P0] API Pool 2.0 正式运行** — 独立目录 `/vol1/1000/tool/api-pool2`、unit `api-pool2.service`、端口 5200；Hermes 主路由已切换并验收。
- [x] **[P0] 非 DeepSeek Endpoint fallback 兼容** — 已完成真实端点矩阵、协议转换、工具调用和 Hermes E2E 验收。
- [x] **[P0] 2.0 多模型上游建模** — **不属于当前 2.0 范围，已否决**；不引入 ModelRoute/独立 Upstream，继续一端点一模型。
- [x] **[P1] claude-opus-5 直连失败归纳** — **不扩展为当前 2.0 的通用模型路由/协议适配任务**；只作为非 DeepSeek Endpoint 真实矩阵的一个验证目标。
- [x] **[P0] 统一 reasoning 意图适配** — **不属于当前 2.0 范围，已否决**；本版本只隔离 DeepSeek 专属字段，不做 reasoning wire 统一转换。

- [x] **[P1] 端点自定义 User-Agent（2026-08-17 已部署）** — 端点配置新增 `default_headers`，UI 提供可选 User-Agent；聊天、Models 探活、获取模型、延迟/多模态测试统一应用。实测 `ps.air-outer.com` 默认 UA 返回 401，`hermes-agent/0.20.1` 返回 200 并获取 3 个模型。部署后 16 端点全部带 `default_headers` 字段（存量端点 `{}` 向后兼容）；service 重启后新进程无 ERROR。
