# API Pool 项目卡片

## 📋 项目卡片

| 字段 | 值 |
|------|------|
| **领域** | 基础设施 — DeepSeek API 端点集中管理 |
| **定位** | DeepSeek 端点集中管理工具，对外统一 OpenAI 兼容接口 |
| **当前阶段** | 功能维护 |
| **状态** | 🟢 运行中 |
| **源码** | `/vol1/1000/tool/api-pool/` (宿主机) |
| **Git remote** | `github.com/849506054/api-pool` |
| **端口** | 5100 |
| **Python** | 3.13 (宿主机默认) |
| **协议** | OpenAI 兼容 + Anthropic (端点级 protocol 属性) |
| **健康检测** | chat ping / models 探针 (端点级 health_mode) |
| **故障转移** | 优先级轮选 → _try_endpoint 内部重试 → 冻结冷却 → 轮转(tried 重置) → 恢复自动回迁 |
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
| 2026-08-05 | _on_success 冷却期间不清除冷却 | 并发请求穿透冷却保护: Ark 429 后冷却被迟到成功请求清除，导致冷却→清冷却→再429 无限循环 |
- [x] **聚合池自动刷新** — 恢复 5s 间隔状态轮询
- [x] **cooldown_minutes 最低 1** — 不允许 0/负数，保证出错端点走冷却→探活→清除完整恢复流程 (commit 3264380)
- [x] **clear-error 独立解冻 API** — `POST /api/endpoints/:id/clear-error` 只清运行态不改配置，前端按钮同步改造 (commit 3264380)
- [x] **reasoning_text 仅 DeepSeek 模型注入** — 非 DeepSeek 端点跳过，避免无关字段导致兼容性问题 (commit 3264380)
- [x] **auto-strip temperature/top_p** — 端点报 400 且含 temperature/top_p 时自动移除重试 (cherry-pick 上游 6e7531f, commit 8c58aef)
- [x] **tool_call_id_prefix 端点可选项** — Kcne 400 根因修复：tool_call id 格式校验（DeepSeek 官方要求 call_00_ET_ 前缀，跨端点切换混入其他端点格式 id → 400）。Endpoint 新增 tool_call_id_prefix 字段（默认空=不重写），chat() 内确定性重写 id 保配对，Kcne 配 call_00_ET_。17:23 部署真实切换验证通过 (commit 9303572)
- [x] **删除 reasoning_text 注入逻辑** — 8/6+8/7 注入修复经 H/K 测试矩阵证伪（Kcne 校验 tool_call id 与 reasoning 字段无关），注入禁用后 Kcne 46万token 请求正常，正式删除 (commit 4626f16)

### 待办

- [x] **[P3] 提交未 commit 的本地改动** — 已随 9303572/4626f16 提交（含探活竞态去重补丁）
- [ ] **[P3] 同步 systemd dropin 配置**
- [ ] **[P2] 待评估：频繁切换端点导致 cache 命中率骤降、增加成本** — 短暂故障应优先原地重试而不是立刻切换，避免丢缓存。可能方案：降权不冻结、首包超时 120s→30~45s、连续 N 次失败才切换 — `proxy.conf` 不在版本控制中，建议文档化或提交模板

- [x] **前后端分离** — GUI 从 `GUI_HTML` 常量抽离至 `static/index.html`，服务端 mtime 缓存读取（改文件热更新，前端改动免重启）；删除 GUI_HTML 常量（commit 1996320）
- [x] **日志 flush + 流式超时兜底补洞（2026-08-15，待重启生效）** — ① `sys_log` print 加 `flush=True`：systemd 下 stdout 块缓冲导致 journal 延迟 6-12 分钟落盘（23:53 的日志 23:52:53 才批量落盘，排障严重误导）；② 首包预读 `settimeout(_first_pkt_timeout)` 改为 `min(_first_pkt_timeout, ep.timeout)`，不再把 socket 阻塞窗口放大到 120s（ep.timeout 仅 60s）；③ `_get_resp_socket` 返回 None 时显式 WARN（原静默跳过）；④ 流内 `except Exception: pass` 改为区分记录：客户端断开(ConnectionResetError/BrokenPipeError)→WARN，上游异常→ERROR（23:58:26 假死请求"收到后无任何日志"即此缺陷）。已 py_compile + test_stream_timeout.py 三场景验证（normal/stall/keepalive 超时链路正常）。文件已推回宿主机（备份 .bak.20260815），**待手动重启 api-pool.service 生效**
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

| 2026-07-05 | tool 消息缺失 tool_calls 时自动补 assistant 回复 | Hermes 插件层消息序列异常导致 Kcne/DeepSeek 400，在 pool.chat() 预处理阶段自动修复
| 2026-07-05 | 移除瞬态路径探活，失败直接重试 | 探活请求浪费 Kcne RPM 配额，且 ping 通过后真实请求仍大概率失败
| 2026-07-05 | 轮转后 `tried=0` 重置 | `tried` 累积导致循环提前退出，剩余可用端点被跳过
| 2026-07-05 | 移除 `chat()` 层瞬态重试，只留 `_try_endpoint` 内部 `max_retries` | 两层重试重叠：内部 2 次 + chat 层 2 次 = 4 次/240s/4RPM，统一为 2 次/120s/2RPM

| 2026-08-04 | chat() 注入 reasoning_text 替代 reasoning_content | DeepSeek V4 request 字段名是 reasoning_text，reasoning_content 导致 Kcne HTTP 400 |
| 2026-08-04 | _on_success 同时缓存 reasoning_text | 响应含 reasoning_text 时直接缓存，否则从 reasoning_content 映射 |
| 2026-08-04 | 端点假成功检测（`check_fake_success` 字段，默认关闭） | 上游返回 200 OK 但内容含"无法给到相关内容"等拒绝信息，按端点开关触发轮转+冷却 |
| 2026-08-04 | 聚合池卡片自动刷新（5s 间隔） | 之前因性能考虑移除全局定时刷新，导致状态需手动刷新页面 |
| 2026-08-04 | 移动端 UI 适配（768px/480px/380px 三断点） | 手机端操作时布局错乱，按钮溢出，表单无法正常填写 |
| 2026-08-07 | cooldown_minutes 最低 1 + clear-error 独立 API | cooldown=0 跳过冷却恢复流程导致错误状态残留；解冻不应改配置值 |
| 2026-08-07 | reasoning_text 注入限定 DeepSeek 模型 | 全局注入会导致非 DeepSeek 端点收到无关字段被拒绝 |
| 2026-08-07 | 上游 Responses API 暂不合并 | 上游 thvse/api-pool 已支持 Responses API (94c1764+9423819)，当前使用场景无关，标记备用 |
| 2026-08-07 | tool_call_id_prefix 端点可选项（默认关闭） | Kcne 400 根因是 tool_call id 格式校验非 reasoning 字段；切换端点时重写 id 为 call_00_ET_ 前缀保配对，其他端点零影响 |
| 2026-08-07 | 删除 reasoning_text 注入逻辑 | 注入/清字段/关 thinking 全部证伪（H/K 测试矩阵），只保留 tool_call_id_prefix 重写；_last_reasoning_text 缓存保留无害 |
| 2026-08-07 | Tokenrhythm 优先级 1，Kcne 降为 2 | 用户指定 Tokenrhythm 为当前使用端点（deepseek-official 未出过 400，不配 prefix）|
| 2026-08-13 | ⚠️ **EXPERIMENTAL 实验版本**（极端情况处理，观察期）四层修改一次性落地 | 08-13 08:00-11:28 大面积端点故障 + Hermes 600s 重试并发 → 33 分钟轮转死循环（详见 skill references/probe-pass-real-request-timeout-loop-2026-08-13.md）。修复：① `_set_cooldown` 幂等化（并发失败不刷新冷却窗口）② chat() 循环顶部冷却跳过（并发请求立即转向）③ 下一级探活失败后并发探活剩余端点（`_check_one_health` 两阶段 11s/21s）④ **prio99 终极兜底**：priority=99 端点正常参与轮换（排最末），全池故障/轮转超 530s 时锁定兜底（60s 容错，530+60=590 < Hermes 600s），成功后续请求 5min 滑动窗口直连，保底失败返回错误走 Hermes fallback（详见 skill references/prio99-fallback-design-2026-08-13.md）|
| 2026-08-13 | deepseek-official priority 5→99（终极兜底端点） | 正常参与轮换（排最末），全池故障/530s 超时锁定兜底。池内优先级：Tokenrhythm=1 / Kcne=2 / kuapi=3 / X5m5x=4 / deepseek-official=99。落盘 api_config.json |
| 2026-08-14 | 单请求饿死判定（skip_cooldown） | 超时类失败但端点在 timeout 窗口内有成功响应 → 并发挤压单请求饿死，非端点故障，仅轮转不冻结（Opencode 连接超时分析结论） |
| 2026-08-13 | 冷却恢复探活后台化（commit 5769d32） | `_cleanup_expired_cooldowns` 同步探活 → 后台入队（`_probe_executor` max_workers=3 + `_probe_inflight` 去重）。请求路径（chat/list_endpoints/get_active_chain）不再被冷却过期端点探活阻塞（原实现多端点串行探活每个最长 10s，前端 5s 轮询"轮流上阵"卡顿）。`_background_probe`：通过清冷却+defer 判断+更新 current / 失败续冷 / 异常兜底。defer 延迟切换保 cache 逻辑完整保留（池活跃恢复端点延迟 5min）。设计确认：后台探活与真实请求并发无害，inflight 只防重复探活不锁真实请求。5 场景烟测 + 重启 active |

## 📌 活跃事项

- [ ] **[P3] 文档化 proxy.conf dropin** — `/etc/systemd/system/api-pool.service.d/proxy.conf` 为系统代理环境变量，建议在仓库中保留模板或注释
