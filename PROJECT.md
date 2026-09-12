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

- [x] **聚合链组内排序与分组联动（2026-09-12）** — 聚合链顺序及优先级徽标统一读取当前组 `priority_by_group`；聚合池切组同步聚合链，后者保留独立选择。组实体提供空组/全禁用组标签。本地快照热更新，前端 hash `bb2f2c06`；`node test/test_chain_group_ui.js` 及生产七组快照对照通过。
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
- [x] **出站 User-Agent 透传（2026-09-05）** — 代理主链路出站 UA 由写死改为透传客户端原请求 UA，端点自定义 UA 仍最高优先，客户端缺 UA 回退默认库标识；管理页与探活保持默认标识。11 单测 + 7 项 mock E2E，生产 hash `3f1babad`。
- [x] **组 fallback 徽标改实时锁定语义（2026-09-07）** — ↩N 历史累计计数整体移除（UI 徽标 + 后端 `_group_fallback_count` 字段/两处 +1/快照 counts 键）；UI 改为锁定期实时徽标 `↩main`。`/api/chain` groups 暴露 `fallback_lock_remaining`（>0 = 该组正整组 fallback 借道 main，即组级延迟回切锁锁定期）；旧快照 counts 残留启动恢复时剔除回写。全套 258 测试通过 + mock E2E，生产 hash 后端 `97d3db7c` / 前端 `b5120044`。
- [x] **启动批量加载保持 config 组内优先级（2026-09-07）** — `add_endpoint()` 每次加入端点即调用 `_renumber_pool_priorities()`，启动加载 26 个端点逐个 add 时以加载顺序为并列 tiebreak 增量重编号，覆盖 config 已保存的组内优先级（Tokenrhythm config bg/cron=2 因 add 序靠前被提为 #1，真正的 #1 ARZ-ds4f/ARP 被挤成 #2）。修复：批量加载路径 `renumber=False`，端点与组实体全部就绪后单次全量重排（config pbg 完整时幂等）；运行期单端点增删保持原增量语义。全套 259 测试通过（含新增回归 + 无 pbg 场景断言更新为按全局序分配），生产 hash 后端 `eb6d43e7`。
- [x] **端点列表当前端点徽章带分组名（2026-09-07）** — 端点列表「● 当前」徽章改为「● 组名」（复用聚合池 `● + 组名` 组合显示，多组 `·` 连接）：main 保持绿色 `● MAIN`，子组用紫色（与聚合池「其他组当前端点」紫徽章同款配色），title 显示完整组名列表。纯前端热更新，生产前端 hash `56254391`。
- [x] **对话日志移动端列宽定稿 + 端点表单名称字段调整（2026-09-07）** — 移动端（≤768px）对话日志表 时间/池组/耗时/缓存命中/推理强度/Tokens 六列固定 60px 等宽（对齐 PC 端固定宽度语义），端点/模型弹性列（min 80px）均分余量；整表 min-width 780→620px、列间隙 8→6px，减少窄屏横向拖动距离。添加/编辑端点表单「站点名称」置前、原「名称」更名「端点名称」且移动端两字段同行两列；站点名称复用端点名称的 URL 自动识别补全机制（`autoFillFromProvider`）。本地浏览器 mock 验证列宽/自动补全后纯前端热更新（static/ 直推，免重启），生产前端 hash `40488d0c`。
- [x] **端点表单移动端按钮同行等高均分（2026-09-07）** — 添加/编辑端点弹窗「取消/保存」移动端（≤768px）由纵向堆叠改为同行显示：按钮 `flex:1` 自适应均分整行宽度（隐藏的批量添加不占位）、高 38px 与输入框同步（`box-sizing:border-box`），分组/加池弹窗同类表单同步生效；PC 端不受影响。mock+浏览器两端实测后纯前端热更新（static/ 直推，免重启），生产前端 hash `2f2a4bbd`。
- [x] **端点保存改字段级 diff + 编辑模式禁用名称自动补全（2026-09-07）** — AgentRouter-glm 端点被误改名为 Air-outer（同 id 未被删除、token 统计被 rename）：根因=编辑弹窗整单 PUT 全字段 + 名称/站点名在名称为空时会被 `detectProvider()` 从 URL 自动补全（添加模式产物 Air-outer），保存时后端 `update_endpoint` 全字段 setattr 覆盖。修复：①`saveEndpoint` 改为字段级 diff——只提交与打开弹窗时快照不同的字段，未改字段（含名称/站点名称）不进 PUT（服务端本就是字段级 setattr）；②`checkFetchBtn` 检测到编辑模式（`editName` 有值）跳过 autoFill，编辑已有端点绝不改写名称/站点名；③添加模式自动补全保留。浏览器 mock 实测：编辑只改 priority → PUT body 仅 `{"priority":5}`；编辑时 URL input 不再覆盖名称。端点已改回 AgentRouter-glm/AgentRouter（is_vision 还原），264 条 token 历史同步恢复。生产前端 hash `ccc9292d`。

### 待办

- [ ] **[P1] Gemini 原生协议适配（方案阶段，2026-09-12）** — 根因（实测）：`Soleapi-gemini` / `Soleapi-gemini-3.7-flash` 端点只开 Gemini 原生方言，`/v1/chat/completions`、`/v1/responses`、`/v1/messages` 三入口同一把 Key、同一模型全部 404「没有能承接该入口协议的模型」。方案：端点级协议枚举新增 `gemini`，出站走 `/v1beta/models/{model}:generateContent`，两端点 `protocol` 改判为 `gemini`。方案文档 `docs/gemini-protocol-bridge-plan-2026-09-12.md`；8 卡拆分（T1 桥接 helper → T2 非流式响应/usage → T3 流式 → T4 前端下拉 → …）见 `docs/plans/2026-09-12-gemini-protocol-bridge-implementation.md` 与 kanban。状态：方案阶段，工作区后端与生产逐字节一致，早期未接线草稿存 `/opt/data/backups/gemini-bridge-draft-20260912.patch`。
- [x] **[P0] 2.0 非 DeepSeek Endpoint fallback 兼容** — 已部署至 5200；按目标 Endpoint 隔离 DeepSeek reasoning 字段，真实端点矩阵和 Hermes 5200 链路均已验收。
- [x] **[P3] 提交未 commit 的本地改动** — 已随 9303572/4626f16 提交（含探活竞态去重补丁）
- [x] **[P3] systemd 代理环境收口** — `api-pool2.service` 已内置 HTTPS_PROXY / HTTP_PROXY / NO_PROXY，不再依赖已删除的 1.0 drop-in。
- [x] **[P2] 缓存保护与切换策略整合评审（2026-09-01）** — 窗口 8-31 14:00→9-1 22:14（journal 完整段 8-31 18:03→9-1 22:14）：1753 请求 ID、15 次原端点重试全成功、6 个跨端点请求全部由真实故障触发（3× 读写超时 90s×2、2× 503 billing、1× 402 quota 冻结 5h）、0 次非必要切换；chat_logs 2025 成功请求缓存命中 92.5%（journal 段 93.0%），无骤降证据。不新增连续 N 次失败、降权状态或 30–45s 首包阈值；后续仅由真实误切换样本重开。8-31 14:00–18:02 journal 明细因宿主机 18:02:56 重启丢失（journald 仅保留当前 boot），该段成功侧 308 请求/命中 89.6% 由 chat_logs 补齐，故障事件仅存于项目记录（developer 400 轮转、gpt 组冷却 fallback）。
- [x] **[P3] save_config 原子写（2026-08-27 已部署）** — 改为同目录临时文件 + `flush`/`fsync`/`os.replace`，增加进程内写锁与失败清理；写入失败向上抛出，旧配置文件保持不变。3 项回归测试覆盖完整写入、替换失败保护和并发写入；部署后源码 hash 一致，服务重启正常并加载 27 个端点。设计见 docs/upstream-borrow-design-v1.md 改动三。
- [x] **[P2] 确定性抖动冷却**（2026-08-28 已部署）— `_set_cooldown()` 固定时长乘 80–120% 系数（种子 sha256(ep.id+fail_count)，跨重启可复现）；配额/余额/探活短冷却通道不参与；冷却日志展示抖动后真实时长。8 项单测 + mock 上游 E2E。commit 6561ced，宿主机 hash=`637027c83b3fbbc20d546ce478ce8189`
- [x] **[P2] 客户端类错误分类**（2026-08-28 已部署）— HTTP 400/404/413/422 且无瞬态字样：不冻结、fail_count 不增、不探活、不改路由指针（A′ 轮转不记账）；`chat()` 请求级 `client_error_tried` 集合防不冻结路径死循环；客户端错误跳过候选探活；DEBUG trace 增加 `kind=client_error`。temperature/top_p 与 tool_call_id_prefix 特例不动。12 项单测（含真实 HTTP auto-strip 集成）+ mock 上游 E2E 16/16。commit 6550d88

- [x] **前后端分离** — GUI 从 `GUI_HTML` 常量抽离至 `static/index.html`，服务端 mtime 缓存读取（改文件热更新，前端改动免重启）；删除 GUI_HTML 常量（commit 1996320）
- [x] **日志 flush + 流式超时兜底补洞（2026-08-15）** — `sys_log` 强制 flush、首包 socket 超时受 Endpoint timeout 约束、socket 获取失败与流异常不再静默；已部署并纳入 2.0 运行版本。
- [x] **端点列表卡片自适应** — 卡片高度与右侧列（聚合池管理+聚合链）底部对齐（alignCards），列表内部滚动，5s 自动刷新保持滚动位置；全局滚动条深色细窄风格统一（commit 1996320）
- [x] **端点列表卡片固定 16px 高度差修复（2026-08-15，commit 14370cc）** — alignCards 原用右侧**容器** `getBoundingClientRect().bottom` 作对齐目标，容器 bottom 包含聚合链卡片 `margin-bottom:16px`，导致左侧 epCard 被固定拉高 16px（实测 epCard.bottom=934 vs 聚合链视觉底部=918）。改为取右侧**最后一个卡片**的视觉底部（`right.lastElementChild.getBoundingClientRect().bottom`），实测 diff=0。前端静态文件热更新，无需重启
- [x] **[P3] 上游错误体 gzip 未解压导致 last_error 含控制字符**（2026-09-12 已修） — `_try_endpoint` 的 HTTPError 分支用 `e.read().decode('utf-8', errors='ignore')` 直接当文本，上游返回 `Content-Encoding: gzip` 时错误正文变成二进制乱码（实测 Soleapi-gemini：`HTTP 404: \x1f\x8b...`），现在该文本会在端点列表与聚合链两面显示。修法：按 `Content-Encoding` 解压或过滤不可打印字符。

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
| 2026-09-08 | Responses 协议完整适配部署 | 入口支持 `/v1/responses` 非流式/流式、工具调用、图片输入、JSON Schema、`store`/`previous_response_id`；上游端点支持 OpenAI Chat、Anthropic、原生 Responses 三协议桥接，Chat 请求也可跨协议命中 Responses 上游。Hermes codex_responses 全链路（含 easy-input 消息兼容修复）与跨协议 fallback 矩阵实测通过；生产后端 hash `ae93f79c`、前端 hash `b5b3d85e`。 |
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
| 2026-08-31 | 聚合池管理卡片按组呈现切换控制 | 聚合池管理卡片不展示端点延迟；手动切换按钮按当前查看组判断，仅本组当前端点隐藏，其他组当前的共享端点仍可在主池或所属组手动切换。纯前端热更新，按组渲染隔离测试通过。 |
| 2026-08-31 | 对话日志表格列宽定稿 | 桌面端时间、延迟、缓存命中、Tokens 四列统一固定 80px，池组/端点/模型平分剩余宽度；移动端七列均固定 60px，超长内容单行省略，表格保留横向滚动。前端语法与列模板断言通过。 |
| 2026-09-01 | 探活失败阶梯冷却（线性递增） | 短时间内探活失败多次重试探活的端点需阶梯式延长冻结时间：请求路径探活失败 30s×连续失败次数（封顶 30 分钟），后台探活失败 cooldown_minutes×抖动×连续失败次数（封顶 1 小时）；`_fail_count` 驱动、探活通过/请求成功清零复位；配额/余额/429 Retry-After 通道不受影响。生产 hash `1cc570e3`。 |
| 2026-09-01 | 普通故障阶梯冷却（线性递增） | 普通故障（5xx 重试耗尽、连接错误、假成功等）冻结时长在原有抖动机制上叠加阶梯：cooldown_minutes×抖动×连续失败次数（封顶 1 小时），仅乘系数 n、原抖动（80–120%、sha256 种子、防惊群）完全保留；成功复位。探活阶梯先行部署（1cc570e3），本轮合并为完整阶梯体系。生产 hash `de58a08c`。 |
| 2026-09-01 | 冷却/冻结状态快照持久化 | 冷却状态重启丢失（402 冻结 18000s 被重启清空→端点被重新使用）。与端点指针同机制：SIGTERM 快照写入 `api_runtime_state.json` 的 `cooldowns` 键（cooldown_until/reason、manual_unlock_required、fail_count、探活 bad），启动恢复、过期不恢复、端点缺失键自愈；`save_runtime_state_groups` 改合并保存（手动切换不抹冷却态）+ `replace_groups` 精确覆盖（快照/清理语义）；defer/展示字段不持久化。222 测试全绿。生产 hash `324eb7be`。 |
| 2026-09-05 | 专用模型组允许复用主池当前端点；对话日志展示真实推理 token | 专用组不再受 main 粘性/在途互斥约束，保持组内 fallback 逻辑；日志从上游 usage.completion_tokens_details.reasoning_tokens 读取并持久化/展示，缺失时显示 —，不使用请求侧推断。 |
| 2026-09-04 | 前端 UI 修复批次：保存反馈 + 端点表单布局重构 | 保存反馈：`api()` fetch 封装从不检查 response.ok → 4xx/5xx 被当成功解析、保存无条件弹成功并关窗；修复=api() 统一返回 `{ok:false,error,status}` + 调用方 toast 错误并保留弹窗（commit 0ac5681，生产 hash b3c0adae）。表单重构：新增端点默认流式总时长 120→0（默认不限）；移动端流式超时三参数同行；端点表单按配置语义分组（路由优先级/请求约束/ToolCall 兼容/额度与频率/协议与能力，ToolCall ID 前缀独占一行）；控件高度统一 38px（`box-sizing:border-box`，桌面/移动端共用）（commit 3bdacdc，22:21 热更新生产 hash cb819950）。纯前端 static/ 直推热更新，无需重启 api-pool2.service。 |
| 2026-09-05 | 出站 User-Agent 透传客户端原请求 | 代理主链路 `_try_endpoint` 出站 UA 不再写死，改为 `resolve_outbound_user_agent()`：优先级 端点自定义 UA（default_headers/extra_headers）> 客户端原请求 UA > 默认库标识 `OpenAI/Python 2.33.0`。客户端 UA 经 `threading.local` 从 `Handler.do_POST` 传递，仅代理路径（`/v1/chat/completions`、`/chat/completions`）写入、`finally` 清理；ThreadingHTTPServer 每请求独占线程，覆盖流式生成器与内部重试。管理页 `fetch_models`/`test-model`/`test-vision` 与后台探活保持默认标识，避免把浏览器 UA 透到上游。客户端缺 UA 时保留默认标识兜底，防止裸 `Python-urllib` 被 WAF 拦。11 项单测 + 7 项 mock 上游 E2E（含流式、跨请求无泄漏、探活/拉模型不透传）。生产 hash `3f1babad`（前端 `f09dc685`）。 |
| 2026-09-06 | 截断类流事务中止统一发可见 error finish | `_timeout_abort` 的可见原因门禁由「仅总时长超限」扩为 `_TRUNCATED_STREAM_REASONS`（总时长超限、业务增量停滞、无新数据停滞）：已向下游输出内容后再中止，回答必然不完整，下游需要 `finish_reason:"error"` + `[API Pool Error: …，输出已截断]` 才能识别。Hermes `chat_completion_helpers.py:4490` 对有文本的 `stop` 按完整回答收尾，续写路径不触发；error finish 经 `_build_partial_stream_stub` 转 `finish_reason=length` 进入续写。触发样本：09-05 19:04 重启后 4 次 `[main]Justwoker` 业务增量停滞（19:51/19:59/20:12/23:39）全部发生在已有输出后（`chat_logs` 18551/18554/18572/18841，completion 3667/65/49/100 字符），且该端点 `stream_max_duration=0` 使唯一可见通道永不触发。不冻结端点、不重放已输出内容、不新增上游重试，边界不动。236 测试全绿（含新增停滞 E2E）。生产 hash `a59a5226`。 |
| 2026-09-06 | 手动切换语义：用户断言优先于观测态 | `switch_to_endpoint()` 原先只清 `_defer_until`，保留冷却/配额/余额冻结 → 对不健康端点是**静默空操作**：`_group_sticky_candidates()` 先按 `_is_in_cooldown` 过滤候选，`chat()` 才读 manual/current 指针，指针根本读不到该端点；请求落到别的端点成功后 `_on_success()` 又因 `_get_manual != ep.id` 抹掉手动覆盖（复现脚本 `tmp-repro/switch_manual_cooldown.py`：切换返回 True、指针已设，实际命中 healthy，manual 归 None；全池冷却时切换后 `chat()` 直接抛「没有可用的 API 端点」）。余额不足端点更是直接返回 `False`，续费后须先点 ⏰ 再点 ⚡。改为把目标端点重置为待验证——清 `_defer_until`/`_cooldown_until`/`_cooldown_reason`/`_fail_count`/`_last_error`/`_last_error_ts`/`_manual_unlock_required`/`_health_error`，`_health="unknown"`——真的发请求，由真实结果重新分类（失败仍走既有故障路径重新冷却）。保留守卫：`enabled`/`in_pool`（配置声明）、`daily_limit`/`rpm_limit` 用量事实；不新增探活。REST 层补 `cooldowns=_collect_cooldown_state()` 落盘，避免崩溃重启从磁盘复活已解除的冻结。前端 ⚡ title 对冷却/锁定端点说明会解除冻结。244 测试全绿（新增 `test_manual_switch_health_reset.py` 8 项）。生产 hash 后端 `dccc0ca2` / 前端 `8a8e615a`。 |
| 2026-09-06 | 定向测试端点结果写回主池端点 | 端点卡片 🧪 测试原先创建临时 `APIPool` 副本请求，成功/失败处置（含 `[req=*] 请求失败` + `触发冷却机制` 日志）全部落在临时对象上，主池真实端点状态不变 → 「日志显示已冻结、端点实际未冻结」假象（触发样本：11:48 `[main]Justwoker` 测试 403 Cloudflare 冷却 5.1 分钟但端点卡片无冷却）。改为 `/api/test` 经 `pool.get_endpoint(id)` 取主池真实端点对象，新增 `APIPool.test_endpoint()`：单端点定向请求（不轮转、不改 current/manual 指针、不选择其他端点），`_apply_test_result()` 按正式故障分类写回——普通错误冷却（reason=`test_failed`）+ 快照落盘、余额不足→`manual_unlock_required`、配额/429 按 Retry-After、客户端类 400/404/413/422 不冻结只记 last_error、成功→`health=ok` 并清冷却/失败计数。聚合池端点卡片新增 🧪 测试按钮（`openTestDrawer` 带 group 参数），与左栏共用同入口。对话日志表头「延迟」改「耗时」（列显示真实请求耗时而非延迟阈值）。7 项新增单测（含 REST 404/落盘断言），全套 251 测试通过 + mock E2E（连接拒绝→冷却 347s 落盘→clear-error 复原）。生产 hash 后端 `dae73b1b` / 前端 `b1f83453`。 |
| 2026-09-06 | 组级 fallback 状态持久化 | 组 fallback 计数（UI 扩容信号）、组 fallback 回切锁（A0 子组→main 滑动窗口）、main 组 prio99 终极兜底锁均为纯内存态，重启全丢 → 重启后 fallback 计数从 0 重新累计（触发样本：12:41:58 重启前 pool-gpt 处于 fallback 锁定中 ~292s，12:46:46 出现「入口 fallback 计数+1」= 计数已归零重来），锁丢失使子组第一波请求先撞子组再 fallback 多一次失败。修复：SIGTERM 快照新增 `group_fallback` 键（counts>0 / locks 未过期 / fallback_locks 未过期），`load_runtime_state` 透传该键（原先只挑 groups/cooldowns 会丢弃未知键）；启动恢复计数与未过期锁（绝对时间戳恢复剩余窗口）、过期锁不恢复、组不存在键自愈剔除并无条件回写（清空也覆盖）。显式写盘（手动切换/定向测试，不传 fallback 参数）合并模式保留文件既有 fallback。6 项新增单测（快照写入/过期剔除/显式写盘保留/重启恢复/残留清理），全套 257 测试通过。生产 hash `00cb6896`；生产验证：旧进程 SIGTERM 快照（13:06:13）仍无 fallback 键（旧代码），新进程第二次重启快照已含键框架（13:08:07），当前窗口内无活跃 fallback 锁属预期（pool-gpt 已回组）。 |
| 2026-09-06 | 移除同模型优先 fallback 分桶 + dedicated 组模型校验 | 两项配套决策，源于渠道模型命名差异事实（deepseek-v4-flash 与 -0731/大小写变体在不同渠道指向同一正式版，字符串相等既漏判又误判；apipool 无法联网查证）。①`_ordered_failover_candidates()` 删除 `prefer_model` 参数与 `ep.model==model` 分桶：失败轮转纯按组内优先级 ring 顺延，同模型/同类型互备由人工把相关端点排成相邻优先级实现（priority 是唯一排序依据，不再被分桶覆盖）。②dedicated 组彻底去掉成员模型校验：删 `_enforce_dedicated_membership()`（入池过滤）、端点改模型自动移出、mixed→dedicated 成员全匹配拒绝、`replace_group_model()` 专用组「仅允许绑定模型」四段；绑定模型保留为 selector/对外暴露名（dedicated 组两点意义：客户端按真实模型名获得专有配置、按能力划分会话工作负载），入池规范由人工遵循，不按规范产生的兼容性问题用户自担。前端组弹窗 tooltip 更新。测试反转 4 项（mismatched_members/set_pool_mismatch/evicts/replace_rejects→allows/keeps/without_check），全套 257 测试通过。 |

| 2026-09-12 | 聚合池与聚合链使用本地快照单向联动 | `refresh()` 保存 `poolSnapshot`，请求代次保护丢弃过期响应；`setPoolGroupFilter()` 本地绘制聚合池后调用 `setChainGroupFilter(poolGroupFilter)`，聚合链仍可独立切组。`renderChain()` 从组定义与成员归属建立标签，先校正选中组，再以当前组优先级同时驱动排序及徽标。缺省组兼容 main，空组/全禁用组呈现空态；排序保持快照原数组。生产前端 md5 前八位 `bb2f2c06`，服务进程保持 `2063577`，HTTP 页面逐字节匹配部署文件。回归包含显式黄金样本、旧版失败对照、生产七组排序与分组切换；自检入口 `node test/test_chain_group_ui.js`。 |
| 2026-09-07 | 移除组 fallback 累计计数徽标与后端计数；UI 改为锁定期实时徽标 `↩main` | ↩N 是「fallback 累计次数」，无时间维度、无法指引当下处理，对用户无意义（同端点累计故障次数）；用户真正需要的是实时状态「该组当前正整组 fallback 借道 main」= 组级延迟回切锁锁定期。后端删除 `_group_fallback_count`（字段/两处 +1/重命名与删组迁移/快照 counts 键），恢复逻辑对旧快照 counts 残留剔除回写；`/api/chain` groups 暴露 `fallback_lock_remaining`（锁剩余秒，>0 锁定中）。前端徽标条件由 `fallback_count>0` 改 `fallback_lock_remaining>0`，文案 `↩main`（title 含剩余时间），组标签 title 同步改实时提示。全套 258 测试通过 + mock E2E（5299 锁定态验证），生产 hash 后端 `97d3db7c` / 前端 `b5120044`。 |
| 2026-09-07 | 启动批量加载保持 config 组内优先级 | 重启后 bg/cron 组 #1 端点在 UI 上变化（用户报告「重启前这两个分别是分组的优先级1」）。根因：`add_endpoint()`（:1237）每次加入端点即 `_renumber_pool_priorities()`，启动加载逐个 add 26 个端点时，重排以「当时已加入成员 + _endpoints 加载顺序」为并列 tiebreak——config 中 bg/cron=2 的 Tokenrhythm 因 add 序第 2 先入组被重排为 #1，真正 #1（bg=AgentRouterZ-ds4f、cron=AgentRouterP，add 序 #20/#22）被挤成 #2；当前端点指针不受影响，故「使用端点正常、优先级不对」。非快照 bug、与 fallback 徽标改动无关（2026-08-29 分组池引入 renumber 即存在），重启暴露。修复：`add_endpoint(ep, renumber=True)` 增加开关，批量加载路径（模块级 4741 + `__init__`）传 `renumber=False`，端点与组实体全部就绪后单次全量重排（config pbg 完整时幂等）；运行期 UI 单端点增删保持增量语义。无 pbg 端点分配语义相应变为按全局 priority 序（原为 add 序），相关测试断言同步更新。全套 259 测试通过，生产 hash 后端 `eb6d43e7`。 |
| 2026-09-07 | 对话日志移动端列宽定稿：时间/池组/耗时/缓存命中/推理强度/Tokens 六列固定 60px 等宽，端点/模型弹性（min 80px），整表 min-width 620px；端点表单站点名称置前、与端点名称同行并共用 URL 自动补全 | 移动端 8 列弹性均分使短列过宽、整表 780px 横向拖动过长；命名信息统一由 URL 识别的 provider 回填，避免手工重复输入 |

### 下一阶段进度（2026-08-31）

- [x] **P0 当前生产增量收口** — 原生模型下拉预取与切换诊断已固化，153 测试通过，commit `163265f`。
- [x] **P1 聚合池分组操作验收** — REST CRUD/落盘、追加式跨组入池、组感知移出、联合重启恢复和前端交互契约已覆盖，157 测试通过，commit `07b7fc1`。
- [x] **P1 分组路由 REST 验收** — 按组手动切换和组内优先级联合场景已覆盖，完整测试 159 项通过，commit `e443cc2`。
- [x] **聚合池管理卡片按组呈现切换控制** — 已移除卡片内端点延迟显示；⚡ 手动切换按钮仅在端点是当前查看组的当前端点时隐藏，按组渲染隔离测试通过。
- [x] **对话日志表格列宽定稿** — 桌面端时间/延迟/缓存命中/Tokens 统一固定 80px，其余三列均分；移动端七列均为 60px，超长数据单行省略。
- [x] **P1 分组 UI 一致性验收（2026-09-01）** — 生产页面执行既有 `refresh()` 后，DOM、`window._poolGroups`、`/api/groups` 与 `/api/chain` 一致：main=5/AgentRouter、pool-bg=5/Opencode、pool-gpt=3/无本组当前；聚合链各组分别渲染 5/5/3，跨组当前 badge 语义正确，页面无横向溢出。此前 main 7 vs 6 为陈旧页面状态，未发现统计口径缺陷；无代码改动、无生产探测。
- [x] **C 重试边界收口（2026-09-01）** — C1 max_retries 归一化 0-3 已完成；C2 继承链核实（图片转译/流式内部重试/temperature 清洗递归显式传 request_id/request_deadline，bg→main fallback 共享预算）；C3 由 P2 扩展窗口结案（13 次超时原端点重试全成功吸收→不停止自动重放）；C4 无 429/恢复冲击证据→不加确定性抖动。无代码改动。

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

- [x] **SQLite WAL + 敏感字过滤 lazy-copy 优化（2026-09-01 已部署）** — chat_logs.db / token_stats.db 切换 `journal_mode=WAL` + `synchronous=NORMAL` + `busy_timeout=5000` （TokenTracker/ChatLogger 新增 `_connect()` 统一连接辅助，每次连接设置；`_init_db` 幂等回 WAL，DB 恢复/拷贝后自动生效）；内容过滤 `filter_payload` 新增 `_has_match` 预扫，无命中直接复用原请求对象、跳过全量深拷贝，命中路径行为与 stats 语义不变。部署 hash `cf0ebae`；全量 201 测试绿（1 个 Hermes E2E 环境依赖失败为存量）；VACUUM/热 journal 运维流程已按 WAL 语义更新（skill references/chat-logs-retention-vacuum-2026-08-15.md）。

- [x] **chat_logs 批量写 + 读路径去锁（2026-09-01 已部署）** — ChatLogger 写入改有界队列（maxsize 512）+ 单批量写线程（攒批上限 50，`executemany` 单事务提交，队列满降级独立线程直写不丢日志）；`get_logs`/`get_log_by_id` 读路径去掉 `self._lock`（WAL 下多读并发安全），`self._lock` 仅串行化写路径。部署 hash `dc2e7283`；全量 208 测试绿（1 个 Hermes E2E 环境依赖失败为存量）。

### 当前阶段：生产请求可靠性与故障判断

当前目标是确保 API Pool 在真实生产请求中的路由、故障判断和响应事务行为正确、稳定、可解释。当前阶段不是性能评测、容量评估或吞吐优化项目。

#### P0：生产请求可靠性

- [x] **路由与恢复语义统一** — 已部署并进入观察。明确区分当前端点粘性、缓存保护与延迟回切、故障冷却和终极兜底锁定；`deferrable` 底层字段兼容保留，产品语义为当前端点缓存保护；自动恢复不覆盖手动指定端点；延迟状态解除后路由指针与实际回切结果一致。
- [x] **探活结果真实性** — 已部署并进入观察。成功但较慢的探活保持 `slow`；未在观察窗口内完成的探活不直接判定为失败；只有明确 `bad` 结果才触发短冷却；后台探活、全量健康检查和请求故障转移遵守端点级去重。
- [x] **流式事务判定正确性** — 已部署并进入观察。明确区分首包超时、连续无新数据停滞、流式总时长超限、Anthropic 提前结束、上游异常和客户端断开；流式事务失败不直接冻结端点；未产生有效输出时允许同端点内部重试一次，已有输出后不透明重放。
- [x] **截断类中止对下游可见（2026-09-06）** — 已有输出后的停滞/超限统一发 `finish_reason:"error"` + 截断标记，Hermes 转 `length` stub 走续写；`_TRUNCATED_STREAM_REASONS` 为可见原因白名单。不冻结端点、不重放。生产 hash `a59a5226`。
- [x] **手动切换优先于观测态（2026-09-06）** — 手动切换把目标端点重置为待验证（清冷却/上游配额/余额冻结与失败计数，`_health="unknown"`）后真的发请求，由真实结果重新分类；不再对不健康端点静默空操作，也不再需要「先解冻再切换」两步。`enabled`/`in_pool` 与用量预算仍为硬性守卫。生产 hash 后端 `dccc0ca2` / 前端 `8a8e615a`。
- [x] **定向测试端点结果写回主池（2026-09-06）** — 端点卡片 🧪 测试改为请求主池真实端点对象（原临时 `APIPool` 副本导致日志显示冷却而主端点状态不变）；失败按正式故障分类写回（冷却/余额冻结/限流 Retry-After/客户端类不冻结），成功复位观测态；测试请求不轮转、不改路由指针。聚合池卡片同步新增 🧪 按钮；对话日志表头「延迟」改「耗时」。251 测试全绿。生产 hash 后端 `dae73b1b` / 前端 `b1f83453`。
- [x] **组级 fallback 状态持久化（2026-09-06）** — 组 fallback 计数、组 fallback 回切锁（A0）、main 组 prio99 兜底锁随 SIGTERM 快照落盘 `group_fallback` 键并在启动恢复（未过期锁按绝对时间戳恢复剩余窗口，过期锁/残留组键剔除）；显式写盘不传 fallback 时保留既有值。重启不再出现 fallback 计数归零重计与锁丢失导致的首请求二次 fallback。257 测试全绿。生产 hash `00cb6896`。
- [x] **复核错误分类与端点处置矩阵** — 已定稿 `docs/error-handling-matrix.md`：19 类错误 × 重试/冷却/探活/切换处置，基于 commit `d5d1f6d` 代码路径与 2026-08-25 部署后真实样本（429/500/503/超时/流式停滞/总时长超限/探活恢复/defer 回切）交叉验证；确认请求级异常不升级为端点级故障。遗留观察项均已解决：手动切换日志已上线（8-31 日志「手动切换端点: '[main]AgentRouter'（清除其 defer...）」，2026-09-06 语义收口后日志改为「按用户断言重置为待验证…」），`_transient_count` 死代码已清理（当前源码无该字段）。
- [x] **请求控制路径可解释性** — DEBUG 请求级诊断已完成。DEBUG 关闭时不增加额外 payload 遍历、网络请求、探针、重试、线程或路由动作；DEBUG 开启时记录端点尝试、内部重试、端点切换和最终状态，不记录完整敏感请求内容，也不逐 chunk 记录流式响应。
- [x] **探活失败阶梯冷却（2026-09-01）** — 探活失败按连续失败次数线性递增冻结时长：请求路径 30s 起步封顶 30 分钟，后台探活 cooldown_minutes×抖动起步封顶 1 小时；成功/探活通过即复位。生产 hash `1cc570e3`。
- [x] **普通故障阶梯冷却（2026-09-01）** — 普通故障（5xx/连接错误/假成功）在原有抖动机制上叠加线性阶梯：cooldown_minutes×抖动×连续失败次数，封顶 1 小时；抖动机制原样保留。生产 hash `de58a08c`。

- [x] **DeepSeek 严格校验 400 防御（2026-09-10，V4.1-Flash 发布日）** — 观测到三类上游校验 400（`reasoning_content must be passed back` / `must be followed by tool messages` / `content-blocked`），同端点相邻请求时通时不通。修复：① 预检修复悬空 tool_calls（`_repair_dangling_tool_calls` 补合成 tool 结果）；② 命中严格校验签名时同端点重试（上限 2 次，第 2 次显式 `thinking: disabled` 绕开回传校验），不直接轮转换模型；inflight 计数在重试前释放防泄漏。270 测试通过（1 个环境相关失败与 HEAD 相同）。生产 hash `5b87d65c`。根因知识 → skill `references/deepseek-reasoning-400-2026-09-10.md`。

#### P1：真实问题驱动的复核

- [x] **观察本轮三个修复的实际行为** — 已结案（2026-08-28）。48h 观察窗口干净：手动切换正确清 defer 并保留冷却状态；AgentRouter 402 配额词典修复（commit 8751b32）经生产真实触发验证——402 正确分类为 `quota_exceeded`，冻结 18000s（默认 5h，响应无 retry-after），不再出现短冷却循环；流式停滞 3 次均正确判「流式事务失败，不冻结端点」，无误冻结样本。
- [x] **复核故障转移控制路径** — 已结案（2026-08-28）。48h 窗口内无 failover 控制路径异常样本，未触发复核条件，按真实问题驱动边界保持现状；实测一次手动指定端点 → 402 → 配额冻结 → 候选探活 → Opencode 兜底成功的完整链路，行为符合设计。（组合行为矩阵见 P2 整合评审 §6，「不加复杂路由状态机」仍为硬边界）
- [x] **按真实故障触发复核对话日志管线** — 已结案（2026-08-28）。48h 窗口内无日志写入阻塞、SQLite 锁冲突、后台线程积压或存储异常证据，按边界不改动。

#### 明确移出当前阶段

- [x] **移出性能评测主线** — 当前阶段不开展 payload 规模基准、P50/P95/P99、Hermes 直连对照、固定开销拆分、吞吐/容量评估，也不为这些评测增加常驻计时或网络请求；未来如确有需要，另建独立性能评估阶段。

**阶段边界**：当前阶段只处理 API Pool 生产请求的路由正确性、故障判断、冷却与恢复、缓存保护、fallback 和流式事务完整性。工作由真实故障样本驱动，优先最小确定性修复。DEBUG 仅用于解释已有请求控制路径，不承担性能基准。除非出现明确生产证据，本阶段不进行 payload 基准、吞吐/容量评估、日志管线重构、路由状态机扩张或多模型上游建模。

## 2026-09-10 视觉池组（Vision Pool Group）已部署生产
图片解析调度改造：组实体新增角色 `role: vision`（组 api-pool-vision，selector api-pool-vision），
`_vision_pool_candidates()` 取代「请求组内任意 is_vision 端点」抓取；降级策略 B（池不可用 → WARNING 日志 + 原图直发）；
翻译失败写阶梯冷却（60s→600s）；图片 hash → 描述短 TTL 缓存（300s/256 条）；UI 组弹窗用途单选 + 👁️ 徽章 + 状态行。
生产 hash 后端 db0f5b35 / 前端 c170fb62（部署前备份 .bak-20260910-195900-vision-pool），git c6f8c58。
池成员：Qwen/Qwen3-VL-32B-Instruct（priority 1，首选）+ Qwen3-VL-8B（priority 2，备选），in_pool=true 仅属视觉池。
全量 288 测试通过 + 1 已知基线失败；真机验收：pool-bg 发图走视觉池转译、目标端点正确回答、同图重发缓存命中（1.1s）。
详见 skill api-pool-management references/vision-pool-group-implemented-2026-09-10.md。

## 2026-09-10 客户端伪装（Client Profile）已部署生产
端点级出站客户端伪装：`client_profile` 三态（`""` 透传默认 / `auto` 按入站 UA 动态识别 / 静态 profile），
内建 hermes profile（12 头完整 X-Stainless 集，`Accept-Encoding: gzip, deflate` + 响应解压链 `_DecodedResponse`），
管理 API 4 路由（GET/POST/PUT/DELETE /api/client-profiles）+ 前端「客户端伪装」下拉 / ⚙️ 管理弹层 / 🎭 徽章。
41 端点零迁移（默认全透传，出站头与部署前逐字节一致）。
生产 hash 后端 0dc01558 / 前端 a0726603（部署前备份 .bak-20260910-233015）。
验证：新单测 30 例全过 + 全量回归无新增；ps.air 实测上游 UA 校验（裸请求 401 / 带 hermes UA 200）；经服务真实请求 200。
详见 skill api-pool-management references/client-impersonation-implemented-2026-09-10.md。

## 2026-09-11 异常统计卡筛选已部署生产
顶部「异常」统计卡变为筛选入口：存在 `health==='bad'` 端点时卡片显示为可点击（手型 + 悬停提示「点击仅显示异常端点」），
点击后端点列表只显示异常端点、卡片红色高亮表示筛选中，再次点击返回全部（「全部」按钮同样可复位）；异常数归零时自动回落到全部。
口径沿用 `renderStats` 既有定义（全部端点中 `health==='bad'`），与站点筛选互斥、站点筛选行为不变。
生产 hash 前端 169935a8（部署前备份 .bak-20260911-173950-abnormal-filter），纯前端热更新免重启。
验证：`node --check` 抽出的 `<script>` 块语法通过 + 7 项运行时断言（默认态可点、激活态高亮、再点返回、5s 自动刷新不重置、筛选结果等于 bad 集合、无异常自动回落、站点筛选不受影响）。

## 2026-09-11 敏感词过滤管理已部署生产
ContentFilter 管理化：新增 3 管理 API（GET /api/content-filter 读配置+known_targets / PUT 全量保存 / POST /api/content-filter/test 实时预览），
PUT 走 save_content_filter_config：写前时间戳备份（content_filter.json.bak.<ts>）→ 原子写（tmp+flush/fsync+os.replace）→ content_filter.load() 热重载，
结构无效/正则编译失败自动回滚内存副本并返回 400，运行态不降级（此前 _reload() 定义但从未被调用，改 json 需重启才生效，本次顺带消除）。
前端：聚合池操作栏新增「🛡 敏感词过滤」入口（首个全局设置类入口；client-profile 为端点级配置故入口仍在端点表单内）+ 管理弹层（开关 / targets 芯片 / 规则增删 / 实时测试），复用 modal-overlay 模式；测试接口不受 enabled 影响（仅预览规则效果）。
同批端点表单布局：客户端伪装行跨两列占满整行（下拉 + ⚙️ + 自定义 UA 同行 model-row，UA 输入框 flex:1 吃满剩余宽度）；「模型上下文长度 (K) / ToolCall ID 前缀 / 计费方式」三列同行（form-row-routing）。
管理弹层控件样式与端点表单统一：规则类型下拉/输入框对齐 34px 高、10px 圆角、13px 字号，类型文案「敏感词 / 正则」，规则列表徽章同步中文。
生产部署：api_pool_server.py.bak.20260911_180149 + static/index.html.bak.20260911_180149（部署前备份）。
验证：E2E mock 全绿（新增规则落盘 / 热生效无需重启 / 无效正则 400+回滚 / 结构错误 400 / 关闭后测试仍预览 / 前端断言 8 处 / node --check 通过）+ 生产 curl 验收（服务 active、GET 返回生产词典 2026-08-30b、test 接口、前端新元素、UI 微调断言）。

## 2026-09-11 客户端特征两态模型（本地透明网关）已部署生产
`client_profile` 收敛为两态：空=透传（出站头=客户端入站头副本，剔除池托管头与连接级头，`Accept-Encoding` 收敛 gzip/deflate/identity）/ profile 名=伪装（出站头=该 profile 的 headers 唯一来源，不混入当前客户端指纹头）。
出站头解析统一为 `resolve_outbound_headers`（代理、探活、管理页端点测试、拉模型四条路径同源）；无客户端上下文的路径复用客户端基线（进程内最近一次真实入站头），无基线时回退默认库标识——消除拉模型不携带客户端特征、探活在上游校验客户端身份时误判的缺口。
`hermes` 由硬编码内建改为 `api_config.json` 的 `client_profiles` 条目，全部 profile 均可编辑/覆盖/删除；41 端点默认透传。
profile 管理弹层重做为列表/表单双视图（可编辑已有 profile、显示由 `User-Agent` 解析出的版本标识如 Hermes 0.21.0），风格对齐既有弹层规范；静态文件按 mtime 热重载免重启。
生产 hash 后端 05970e18 / 前端 2c9ae0ea（备份 api_pool_server.py.bak-20260911-clientmode、static/index.html.bak-20260911-clientmode、api_config.json.bak-20260911-pre-flatten）。
验证：隔离实例端到端四项（回显上游实证透传全量头、伪装不混入客户端头、探活复用基线、无基线回退）+ 全量 36 个测试文件 0 失败（原基线 1 例过时断言一并修正）+ 生产真实流量 200 与健康检测无 WARN；前端代码级断言脚本 scripts/verify_client_profile_ui.js。
详见 skill api-pool-management references/client-transparent-gateway-implemented-2026-09-11.md。

## 2026-09-11 GLM-5.3 关闭思考映射 + 严格校验重试收口 已部署生产
现象：`[main]AgentRouter-glm: glm-5.3` 返回 400「该模型始终思考，不支持关闭思考；请使用 low、high 或 max」（req=befc3e5e，22:29:33），此前 22:29:09 曾命中 DeepSeek 严格校验 400 并触发同端点重试。
根因两条：① GLM-5.3 官方契约 `thinking.type` 仅支持 `enabled`（思考不可关闭），池侧只做了 `reasoning_effort` 数值映射，没有处理 `thinking` 形态——官方迁移建议是原发 `{"type":"disabled"}` 的调用方改为 `{"type":"enabled"}` + `reasoning_effort: "low"`；② 2026-09-10 严格校验防御的 `disable_thinking_forced` 是**请求级**标志，置真后轮转中的每个后续端点都被注入 `thinking={"type":"disabled"}`，轮到 GLM 即 400（跨端点污染）。
修复：① 新增 `_normalize_glm_thinking`——GLM-5.3 系列把 `disabled`/`none`/`off` 折叠为 `{"type":"enabled"}` + `reasoning_effort: low`（显式档位不覆盖；数据式映射，与 Hermes `agent/reasoning_effort.py` 的 GLM53/OX_ALPHA 声明同型）；② 删除严格校验 400 的原第 2 次「显式关闭 thinking」重试，同端点重试维持**请求级总上限 1**——该类 400 校验的是历史消息形态（旧轮 assistant 的 `" "` 占位 reasoning_content），请求级 `thinking=disabled` 改不了已发历史，结构性无效（42h 窗口内 0 成功），且该字段会随轮转污染异构端点（本次 GLM 400 的直接触发路径）。
验证：直连对照（未折叠原始 `disabled` 仍 400 原文一致；折叠后 `disabled`/`none`/`off` 均 200；显式 `high` 保留档位 200；preserved_thinking 注入体 200）；全量 36 个测试文件 0 失败（GLM 适配测试新增关闭思考映射与「不再注入 disabled thinking」护栏断言）；生产 hash 后端 `33636e88`；重启后无 ERROR/WARN、请求正常成功。改前备份 `backups/api_pool_server.py.20260911-231948.bak`（本轮首次部署前备份 `backups/api_pool_server.py.20260911-230616.bak`）。

## 2026-09-12 chat/completions → Responses 桥接体形状修复（assistant 正文类型 + reasoning_effort 透传）已部署生产

现象：5200 请求 `gpt-6-astra`（pool-gpt6 组，三端点 `protocol=responses`）连报两类 400 —— ① 默认 openai 协议下 `Function tools with reasoning_effort are not supported for gpt-6-astra in /v1/chat/completions`；② 端点切 responses 后 `Invalid value: 'input_text'. Supported values are: 'output_text' and 'refusal'`。

根因：① 属上游模型能力约束（chat 下 tools 与 reasoning_effort 不可共存，正解为切 `/v1/responses`），池侧无缺陷；② 属池内桥接缺陷 —— `_chat_content_to_responses_content()` 对所有角色硬编码 `input_text`，assistant 历史消息正文（Responses 规范只收 `output_text`/`refusal`）被 ps.air-outer 严格校验拒绝，只在历史含 assistant 消息时触发，故单轮健康探针一直正常而真实流量全 400；三端点全 400 后按候选轮转由 `[main]AgentRouter-ds4f` 返回成功，表现为 200 而 `model=deepseek-v4-flash` 的静默降级。

修复：① `_chat_content_to_responses_content(content, text_type="input_text")`，assistant 分支传 `output_text`（user/system 保持 `input_text`）；② `_responses_body_from_chat` 末端补顶层 `reasoning_effort` → Responses `reasoning.effort` 透传（客户端显式 `reasoning` 优先；`minimal` 上游不收，钳到 `low`），此前该键不在出站白名单、客户端推理强度被静默丢弃。

验证：上游直连对照矩阵（assistant `input_text`+tools 400 与生产报错逐字一致 / `output_text`+tools 200 / 无 assistant 200 / `output_text`+tools+`reasoning.effort=high` 200；档位枚举 none/low/medium/high/xhigh/max/disabled 全 200，仅 `minimal` 400）；隔离实例对照组（未打补丁 3 条 400 ERROR + 降级 ds4f，打补丁 0 ERROR 首端点成功）；mock 上游捕获桥接后真实出站体（assistant=`output_text`、user=`input_text`、`reasoning={"effort":"medium"}`）；生产 5200 以「tools + assistant 历史 + `reasoning_effort=medium`」请求实测 `[pool-gpt6]AgentRouterP-gpt-6-astra` 首端点成功且回包 `model=gpt-6-astra`；全量 38 个测试文件 0 失败（新增 `test/test_responses_chat_bridge_shape.py`）。

生产 hash 后端 `99dccd6a`（改前 `b6d8f17a`），备份 `api_pool_server.py.bak-20260912-0140-responses-bridge`，重启时间 2026-09-12 01:39。验证工具 `scripts/mock_responses_capture.py` / `scripts/fire_hermes_shape.py`；排查笔记见 skill references/responses-bridge-shape-fix-2026-09-12.md。

## 2026-09-12 vision 池升为 main 同级内置组 + 组级上下文长度（纯显式）已部署生产

vision 池定位修正：由「`role: vision` 标记的普通组」改为与 main 同级的系统内置池——系统内唯一、编辑分组弹窗的「用途（role）」配置项删除、前端聚合池与聚合链标签栏与 main 同位（🏊 main → 👁️ vision 恒置顶，同去专属蓝色徽章）。后端新增常量 `VISION_GROUP` / `VISION_SELECTOR`，加载时恒建 main(首位)+vision(次位)，旧 `role` 键一律忽略；内置组名称/类型/选择器/删除全锁（建同名报「保留名」）；`/api/groups`、`/api/chain` 以 `is_vision` 取代 `role`。

组级上下文长度（池组声明 → Hermes 读取）：组实体新增 `context_k`（K=1000 tokens，0=不声明，边界 2K–10000K，纯显式无推导兜底）；对外经 `GET /v1/models` 的 `context_length` 暴露，新增 `GET /v1/models/{selector}`（未知 404），`GET /api/groups` 暴露 `context_k`，落盘 `pool_group_defs[].context_k` 并在加载时恢复（含 main）；main/vision 的 `context_k` 可改。前端分组弹窗新增「上下文长度 (K)」输入 + 原生 datalist 档位 256/400/512/1000K（可手填）。

部署：后端 `b6d8f17a` / 前端 `c6e3d153`（01:27 重启；档位精简 01:30 热更新）。生产已填组值：main/pool-bg/pool-cron/pool-gpt6/pool-gpt5=1000K、vision=256K，`/v1/models` 已带 `context_length`。Hermes 侧 `config.yaml` 的 `model.context_length` 保持显式声明不变（该键是 Hermes 解析链第 1 级，未删除前池声明不生效；口径与判定依据见 skill references/group-context-population-2026-09-12.md）。

验证：新增 `test/test_group_context_length.py` 8 项，全量 320 测试 0 失败（另 1 项预存环境错误：缺 `openai` 包）；隔离实例（5299 + HERMES_HOME 隔离）实测池值被 Hermes 真实解析器读出。工作区提交 `3bc75ee`。

## 2026-09-12 Responses 协议上游 usage 记账修复（缓存命中率恒 0 + 推理保真）已部署生产

现象：飞书端窗口观察到 `gpt-6-astra`（pool-gpt6 组，三端点 `protocol=responses`）在池 UI 上缓存命中率恒 0，并附「推理也全为空」。

根因（分流判定为**池侧记账缺陷**，非上游）：① 流式 responses 分支（`_try_endpoint` 的 `response.completed` 处理）只解析 `input_tokens`/`output_tokens`/`total_tokens`，**从不读 `input_tokens_details.cached_tokens`**，而紧随其后的 `has_usage` 判定正是据这三个值置真并记账 → `token_usage.cached_tokens` 恒 0，且返回客户端的 usage 帧也没有 `prompt_tokens_details`；② 非流式 responses 分支直接 `return` 客户端响应，**没有任何 `add_usage`/`add_log` 调用**（对比 anthropic 分支有），连 token_stats 行都不写。佐证：生产 token_stats 内 7 条 gpt-6 记录全为流式且 cached 全 0；deepseek 端点命中率显示正常，因其走 chat 协议分支（解析+记账完整）。上游确有缓存：直连与经池「同 prompt 对拍」命中节奏完全一致（池未破坏缓存 key，`tool_call_id_prefix` 为空）。

修复：① 流式 `response.completed` 解析 `input_tokens_details.cached_tokens` 与 `output_tokens_details.reasoning_tokens`，客户端 usage 帧补 `prompt_tokens_details`/`completion_tokens_details`；② 非流式 responses 分支补 `token_tracker.add_usage` + `chat_logger.add_log` + `_mark_cache_stats_account`；③ 推理保真：桥接体 `reasoning` 追加 `summary: "auto"`（Responses 只在显式请求摘要时返回推理文本），流式识别 `response.reasoning_summary_text.delta`/`response.reasoning_text.delta` → chat `reasoning_content`，并以 `response.output_item.done` 的 reasoning 摘要作兜底（不重复下发），非流式 `_responses_output_to_chat_message` 收集 reasoning item 摘要 → `message["reasoning_content"]`（返回三元组，两个调用点同步更新）。

验证：隔离 A/B（mock 上游返回 cached=1234/reasoning=56）旧码仅 1 行且 cached=0、非流式无行、客户端 usage 无 details；新码 2 行、cached=1234、reasoning=56、客户端带 details。回归 `test_responses_api.py` 新增 `responses upstream usage accounting`（旧码 FAIL / 新码 PASS，含推理 delta 透传与 `summary:auto` 断言），responses 套件 8/8、全量 38 个测试文件 0 失败、ruff 127 条与基线一致。生产实测（5200，增长式多轮流式）：`#1 cached=0 → #2 cached=10811 → #3 10825 → #4 10839`，token_stats/chat_logs 按真实值落盘。上游真实行为（直连实测）：缓存命中**不稳定**（`#1 write 21607 → #2 cached 21607 → #3/#4 整段重写 → #5 cached 86449`；同站点 deepseek 每轮稳定命中 17k），故修复后 UI 显示的是真实且波动的命中率；推理文本方面，上游**仅在请求体带 `reasoning.summary` 时下发推理摘要**（实测流式 `response.reasoning_summary_text.delta` ×99 / 452 字符；不请求时只有不可读 `encrypted_content`，同一形态偶发零事件），chat 协议则完全不返回 `reasoning_content`——修复后生产流式已实测透出 `reasoning_content`（474 字符），chat_logs `reasoning_tokens=86`。

生产 hash 后端 `f7aff399`（改前 `99dccd6a`），备份 `api_pool_server.py.bak-20260912-0217-usage-accounting`，重启 2026-09-12 02:16。验证工具 `scripts/probe_prefix_growth.py`、`scripts/mock_responses_usage.py`、`scripts/check_accounting.py`、`scripts/run_accounting_trial.sh`、`scripts/verify_prod_hit_accounting.py`；排查笔记见 skill references/responses-usage-accounting-2026-09-12.md。

## 2026-09-12 锁定日志降噪（与端点 defer 同构：锁定期路由静默）已部署生产

现象：`组 'pool-gpt6' fallback 锁定中（剩余 Ns），本请求走 main 组` 逐请求刷屏——近 24h **235 行**（`pool-gpt6` 227 + `pool-bg` 8），近 1h 52 行（约每 15 秒一行）。

根因：锁定分支位于滑动窗口顺延循环内、命中即打日志，而滑动锁又被每次请求顺延 → 锁期内请求越密刷得越勤。

修复（两段收敛，终态=与端点延迟切换同构）：先按「每锁定周期只报一次」去重（`_group_fallback_lock_logged`，hash `d98f89f2`）；随后按用户口径定稿——**整组 fallback 延迟回切与端点 defer 是同一类语义（延迟回迁窗口保护当前工作对象），日志处理同构即可**：锁定期路由**完全静默**走 main（删除该 INFO 行与去重标记，回退到无日志形态），状态经 `/api/chain` 的 `fallback_lock_remaining` + `↩main` 徽标呈现（与端点 defer 的 `is_deferred`/`defer_remaining` 同一模式），转移事件由入口/耗尽 fallback 两条 WARN 行承担。

边界：`↩N` 累计计数已于 **2026-09-07 整体移除**（后端 `_group_fallback_count`、`/api/chain` 的 `counts`、前端 `↩N` 全删），该计数属已废弃设计 → 降噪无任何保留语义损失。

验证：A/B（旧码 5 请求 → 5 行；中间版 → 1 行；终态 → 0 行，转移 WARN 保留 1 条）；回归测试 `test_locked_group_is_silent_per_request`（旧码 FAIL / 新码 PASS，含「转移 WARN 保留」断言）；全量 38 测试文件 0 失败；生产验证：重启后连发 3 个 gpt-6 请求（组处于锁定态），「锁定中」行数为 **0**。

生产 hash 后端 `48833601`（改前 `d98f89f2`→`f7aff399`），备份 `api_pool_server.py.bak-20260912-0310-locklog-silent`，重启 2026-09-12 03:10。排查签名见 skill references/incident-pattern-signatures.md「锁定日志逐请求刷屏」节（已更新为终态）。

## 2026-09-12 整组 fallback 手动「立即切回」（↩main / ⏸待回切 徽标可点）已部署生产

需求（用户）：整组 fallback 增加手动解除行为（方式为点击 `↩main`）；以及 fallback 时间结束后同步显示延迟切换状态徽标，点击徽标立即切回。用户定稿口径：①期满后要保留可见的是「锁已期满、尚无该组请求回组试探」的**待回切**中间态；②「立即切回」= 清回切锁 **+** 清该组成员的冷却/冻结/失败态（同「手动切换端点」的用户断言语义），让下一个请求真的落回本组。

改动：
- `APIPool.clear_group_fallback(group)`：清该组回切锁（滑动空闲窗口立即期满）+ 按用户断言重置该组成员端点状态（抽出 `_reset_endpoint_assertion_state()`，与 `switch_to_endpoint()` 共用同一套重置）；`_defer_until` 保留——defer 是当前工作对象的缓存保护而非失败态，且一个端点可属多组，清掉会误伤别组正在工作的对象。只清锁不重置端点时，仍全冷的成员端点会立刻重新 fallback，点击等于空操作。
- 新增 `POST /api/groups/<name>/clear-fallback`（组不存在 404）；动作生效时以 `cooldowns` + `group_fallback` 精确覆盖落盘（崩溃重启不复活已解除的锁/冷却）。与「手动切换端点」的落盘处理同口径。
- 新增 `_group_fallback_pending`「待回切」态：两处 fallback 触发点（入口无可用端点 / 轮转耗尽）置位；锁期满后该组首个请求清除（回切已发生）。不落盘：重启后锁已归零、下一个请求本就回组，落盘只会显示一个不存在的状态。重命名/删组同步迁移清理。
- `/api/chain` groups 新增 `fallback_return_pending`；前端 `↩main`（锁定期，带剩余时间）与 `⏸待回切`（沿用端点 `⏸延迟回切` 的 `badge-deferred` 形态）两处徽标可点、同调一个接口；徽标字号/内边距与同行 `● 当前端点` 对齐（用户当轮指令），组标签 title 同步提示待回切。

验证：新增 `test/test_group_fallback_unlock.py` 5 项——清锁 + 只重置本组成员（别组端点与 defer 不受影响）、无锁阶段点击仍重置端点、重启恢复只保留仍在锁/仍冷却的组、**chat() 端到端：fallback 后点击 → 下一个该组请求落回本组端点**、期满未点击 → 本组请求自行回组并清待回切。全套 343 测试通过（基线 338），`ruff` 基线 125 条不变。生产验证：`/api/chain` 已返回 `fallback_return_pending`；`POST /api/groups/nope-xyz/clear-fallback` → `404 {"error": "组 'nope-xyz' 不存在"}`（未命中路由为 `{"error": "Not found"}`）证明分支已生效；`/` 返回的前端已含 `clearGroupFallback`。对真实组的点击（会解冻该组端点）由用户在生产态执行。

生产 hash 后端 `f6bf97f5`（改前 `48833601`）/ 前端 `7640d0f1`（改前 `c6e3d153`），备份 `api_pool_server.py.bak-20260912-0855-group-fbswitch` / `static/index.html.bak-20260912-0855-group-fbswitch`，重启 2026-09-12 08:43。

## 2026-09-12 端点错误详情完整性 + 异常口径四处统一（epErr）+ 响应体解压补漏 已部署生产

需求（用户）：聚合链里的端点错误信息不完整，且健康状态与聚合池不一致（聚合池看不出异常、聚合链显示 402）；端点列表也只用 ❌ 徽章标记、卡片不高亮，健康异常应当高亮（用户当轮补充）。方案经用户确认后实施（含「错误展示扩到 health_error || last_error」选项）。用户随后定稿：聚合池卡只显示错误高亮边框，**不显示**具体错误详情。第三轮（同日 17:2x）用户反馈：聚合池错误端点仍无红框、聚合链错误端点红块消失、状态卡异常数与实际对不上——根因是四个面各用一套"异常"定义（状态卡 `health==='bad'` 计数、端点列表 `last_error||bad`、池卡仅 `bad`、链红块 `bad||fail_count` 而错误文本却按 `bad?health_error:last_error` 出）。用户定稿口径：**异常 = 健康状态以端点最后可知状态为准**（`epErr = health==='bad' || last_error 非空`），四处统一。另修复 Soleapi 拉模型 gzip 报错（见下）。

改动：
- 后端 `health_error` 一律存**完整**原文，移除 5 处 `[:100]` 截断：`_probe_endpoint`（chat 探针）、`_check_one_health`（chat 探针 `err_str` 与 models 探针 `Models接口错误`）、`_apply_test_result`（🧪 测试写回）、`check_all_health` 异常分支。上游响应体本身已在 `_try_endpoint` 限长 1000 字符，截断属纯信息丢失。
- `clear_error()`（⏰ 手动解冻）补清 `_health_error`：原实现只清 `_last_error` 并置 `_health="ok"`，会留下「✅ + 陈旧错误文本」错位（2026-09-12 16:53 AgentRouterP-gpt6a 实测）。
- `/api/chain` 每项新增 `last_error`，供聚合链兜底显示真实请求失败原因（客户端类 400/404/413/422 只写 `last_error`、不写 `health_error`，此前链上完全看不到这类原因）。
- 前端异常判据统一为 `epErr(ep)`（helper，L758）：状态卡「异常」计数与点击筛选（`renderStats`，全部端点口径含池外）、端点列表卡红框（`renderEndpoints`）、聚合池卡红框（`renderPoolList`，仅红框不渲染徽章/错误行，保持紧凑）、聚合链红块 `chain-item failed`（`renderChain`，优先级：手动解冻 > 冷却 > 延迟回切 > 当前服务中 > 异常）——「有错误文本必有红块」。错误详情只在端点列表与聚合链：列表错误行 `last_error` 为空时回落 `health==='bad' && health_error`；链详情整段换行显示在 info 列（`.chain-err` 去 `max-width:120px` 单行省略），文本 `health==='bad' ? (health_error||last_error) : last_error`。一致性要求（用户 2026-09-12 定稿）：状态卡「异常」数**只需与端点列表实际相符**——两处共用同一 `epErr` 判据、列表默认「全部」含池外端点，因此天然一一对应（现网实测：计数 2 = 列表红框 2，PM/Soleapi-gemini，两个都在池外）。聚合池/聚合链只含池内成员，其红框数不参与该一致性要求（池外异常不显示在池面板是预期行为）。
- **响应体解压补漏（Soleapi 拉模型 `'utf-8' codec can't decode byte 0x8b`）**：`fetch_models` 此前未包 `_DecodedResponse`，透传/客户端基线声明 `Accept-Encoding: gzip, deflate` 后上游真压缩，`json.loads(resp.read().decode("utf-8"))` 直接炸；`HTTPError` 错误体三处（`_try_endpoint`、`/api/endpoints/<id>/models`、`/api/fetch-models`）同样未包，gzip 错误体存进 `last_error` 成二进制乱码（Soleapi 实测 `HTTP 404: \x1f\x8b...`）。4 处读取点全部补包，复用既有解压链，未新增机制。

验证：`py_compile` 通过、`ruff` 基线 125 条不变、inline JS `node --check` 通过；渲染层静态断言 `test/render_error_smoke.js` 扩到 11 项全通过（状态卡 epErr 计数、池卡 bad+last_error 双红框且无详情、列表双高亮与回落、链红块与全文）；既有 `test/test_chain_group_ui.js` 因新增 `epErr` 外部依赖同步修补（切片摘取 helper 源）后 PASS；gzip 回归 `test_decoded_response.py::test_fetch_models_decodes_gzip_json` 在旧码上精确复现同一 `UnicodeDecodeError`、新码通过；全量 **344 tests passed**。生产端到端：`POST /api/test` 触发 AgentRouter 真实失败 → `health_error` 长度 **219**（此前恒 100）；部署后 `GET /api/endpoints/<Soleapi id>/models` → `{"ok": true, "models": [...]}`（修复前同请求返回 `❌ 'utf-8' codec...`）；`/` 前端 md5 与工作区一致；`POST /api/test-pool` 返回 `pong`；重启后 journal 0 ERROR；live 快照渲染核对：状态卡/端点列表/池卡/链四处红块数一致。

生产 hash 后端 `35e24c16`（改前 `f6bf97f5`，中间版 `663da144`/`d19d2b32`）/ 前端 `01fd4e50`（改前 `bb2f2c06`，中间版 `db600866`/`781aaead`），备份 `api_pool_server.py.bak-20260912-170358-chain-error-detail`（原版）/ `...-chain-error-detail-v1` / `...-20260912-172842-errsem-gzip`（gzip 修复前）/ `static/index.html.bak-20260912-170358-chain-error-detail` / `...-20260912-171108-pool-no-detail` / `...-20260912-172842-errsem`（epErr 修复前），重启 2026-09-12 17:04:05 / 17:06:17 / **17:28:42（终态，含 gzip 修复）**；前端静态文件热读，最终版随终态重启一并 scp。

## 2026-09-12 严格校验 400「换形态重试」+ tool_call id 重写非变异 已部署生产

需求（用户）：DeepSeek 严格校验 400（`reasoning_text/reasoning_content ... must be passed back`）的本地防御，在既有「一次重试」上增加**清空前缀再请求一次**的设计，并在真实使用场景中观察效果。用户提供关键事实：事故期间曾改过「重写 ToolCall ID 前缀」（加了又删），也试过把协议改成 Responses。

诊断（证据边界）：17:24:52 起（**早于**当日 17:28:43 部署重启）已有该 400；17:29:12 配置快照 `tool_call_id_prefix='call_00_ET_'`，17:34:02 快照已清空；两次手动切回（17:30:10、17:31:01）之后才在 17:32:17 恢复，14 分钟内 3 笔故障请求的**原样重试全部再次失败**（每笔 2 次尝试）。两份快照 `protocol` 均为 openai → 无法把 Responses 临时切换与恢复时刻对应，**不作归因**；前缀清空与恢复在时间上相符，但样本仅 1 次，定位为「最值得优先验证的变量」而非定论。

设计（已实施）：

| 前提 | 唯一一次重试的形态 | 其余情况 |
|---|---|---|
| 本尝试**确实应用过前缀重写**（`attempt_prefix_applied>0`） | **跳过前缀重写**、改用客户端原始 tool_call id（换一种历史形态） | — |
| 未应用前缀重写 | **不重试**、直接轮转（改回原始 id 无变化＝原样复读） | 打日志后转轮转 |

（用户 2026-09-12 定案：**替换**而非追加——唯一一次重试就是「清空前缀」的换形态请求，
删除原「原样复读」重试；会话内 3 笔故障 6 次尝试全失败，印证原样复读无意义。）

改动：
- `_rewrite_tool_call_ids` 改**非变异**：返回 `(新消息列表, 实际重写条数)`，只复制命中的消息与其 `tool_calls` 列表。原实现浅拷贝后原地改写嵌套 dict → ① 轮转各次尝试共用同一份 Hermes 历史，某端点的重写结果会带给后续异构端点（违反「payload 改写按端点隔离」铁律）；②「改回原始 id 再试一次」在实现上不可能。
- `chat()`：请求级标志 `skip_prefix_rewrite`（换形态开关，**消费一次即清除**，轮转后各端点按自己配置重新决定）；严格校验 400 分支：预算 1，命中且 `attempt_prefix_applied>0` 时发「原始 id」变体重试，否则直接轮转；日志分别记录「改用原始 id 重试」与「未应用前缀重写→转轮转」。
- 明确不做：不自动切换端点 `protocol`；不改端点配置；不恢复 2026-09-11 删除的「thinking=disabled」重试（请求级参数改不了被判定的历史形态，结论仍有效）。

验证：新增 `test/test_strict_validation_prefix_retry.py` 4 例——单元（非变异 + assistant/tool 配对 + 幂等 + changed 计数）；集成用**真实本地 HTTP 上游**（ThreadingHTTPServer，按「历史里是否存在前缀 id」回 400/200）跑 `pool.chat()` 全链路：3 次尝试序列（重写 → 原样 → 原始 id）最终 200 且调用方历史零改动、无前缀时只 1 次原样重试后轮转、前缀重写不泄漏给轮转后的端点。黄金样本：新用例跑在改前文件（`35e24c16`）上 **3/4 失败**，改后 4/4 通过。全量回归 **348 passed**（基线 344）、`py_compile` OK、`ruff` 基线 125 不变、`test/test_glm_reasoning_adaptation.py` 护栏断言同步更新。生产：`/api/endpoints`、`/api/chain` 200，重启后 journal `[ERROR]` 计数 0，`POST /api/test-pool` → `pong`。

生产 hash 后端 `35e24c16` → `e2fe9219`（预算 2 中间版）→ `db05802e`（预算 1 终版，前端本轮未动，仍 `01fd4e50`），备份 `api_pool_server.py.bak-20260912-175606-strict400-idvariant` / `api_pool_server.py.bak-20260912-180558-strict400-budget1`，重启 2026-09-12 18:05:58。

观察口径（待用户在生产真实场景确认）：journal 里统计「改用原始 id 重试」成功率，与「未应用前缀重写→直接轮转」的出现频率。注意当前 `AgentRouter-ds4f` 的 `tool_call_id_prefix` 为空 → 阶段 2 暂不会触发，要观察变体需先给某端点重新配置前缀（配置前建议先按 `tool-call-prefix-direct-probe-2026-08-18.md` 的判定表确认该端点是否真需要前缀）。
