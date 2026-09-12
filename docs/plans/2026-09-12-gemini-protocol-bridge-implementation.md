# Gemini 兼容协议适配 — 开发计划（2026-09-12）

> 设计依据：`docs/gemini-protocol-bridge-plan-2026-09-12.md`（已认可）
> 状态：**计划阶段，不写代码**。代码全部落在工作区 `/opt/data/work/api-pool2/`；
> 生产 `/vol1/1000/tool/api-pool2/` 的写入与服务重启需单独批准（T7）。

**目标**：API Pool 支持端点级 `protocol="gemini"`，让 soleapi 的 Gemini 系模型可用
（入站 OpenAI 形态不变，出站走 `/v1beta/models/{m}:generateContent` 原生方言）。

**架构**：在 `_try_endpoint` 里按协议分支，新增一条「OpenAI chat payload ↔ Gemini 原生」双向转换
通路；tool 续跑靠 `thoughtSignature` 回填（进程内 TTL 表按 pool 生成的 tool_call id 索引）。

**约束**：只影响 `protocol="gemini"` 的端点；其他三条协议路径零改动；纯新增分支，可整段回滚。

---

## 任务总表

- **T1** 桥接 helper + 请求侧接线（URL / body / 鉴权 / 出站头白名单）
- **T2** 非流式响应转换 + usage 记账
- **T3** 流式转换（首包与停滞判定、chunk 映射、收尾帧）
- **T4** 前端协议下拉项
- **T5** 离线假上游单测
- **T6** 本地端到端验证（真实 soleapi，独立实例）
- **T7** 部署 + 端点配置切换 + 灰度观察（**需批准**）
- **S0** 项目跟踪卡（umbrella，`blocked`，不执行）

依赖链：T1 → T2 / T3 → T5 → T6 → T7；T4 与 T2/T3 并行；S0 只做跟踪。

---

## T1 桥接 helper + 请求侧接线

**目标**：端点声明 `protocol="gemini"` 时，出站请求正确变成 Gemini 原生请求。

**文件与位置**：`api_pool_server.py`
- 新增 helper（插在 `class Endpoint` 之前）：`_gemini_url`、`_gemini_payload_from_chat`、
  `_gemini_parts_from_content`、`_gemini_text_from_content`、`_gemini_tools_from_chat`、
  `_gemini_tool_config_from_chat`、`_gemini_parts_to_chat`、`_gemini_finish_reason`、
  `_gemini_usage_to_chat_usage`、`_gemini_remember_tool_call`、`_gemini_lookup_tool_call`
  （草稿在 `/opt/data/backups/gemini-bridge-draft-20260912.patch`，可直接复用）
- `_try_endpoint` 请求构造（现 `~5280` / `~5397`）：`is_gemini` 分支 → URL + body；末尾条件改
  `elif not (is_responses or is_gemini):`
- 出站鉴权：保留 `Authorization`，追加 `x-goog-api-key`
- `_PROFILE_RESERVED_HEADERS` 增加 `x-goog-api-key`

**验收证据**：`ruff check` + `py_compile` 通过；`_gemini_url` 三种 base 写法断言
（`…/v1`、`…/v1beta`、裸域）；`_gemini_payload_from_chat` 对
（system + user + 图片 data URL + tools + tool_choice + generationConfig）样例的产物断言；
`x-goog-api-key` 不在伪装 profile 可覆盖头里。

**依赖**：无。**预估**：helper ~320 行 + 接线 ~30 行。

## T2 非流式响应转换 + usage 记账

**目标**：`generateContent` 响应 → 标准 OpenAI `chat.completion`。

**文件与位置**：`api_pool_server.py` 非流式分支（现 `~5963`，`if is_responses:` 之前加 `if is_gemini:`）；
沿用既有 `check_fake_success` / `token_tracker` / `chat_logger` 记账。

**要点**：`parts` → `content` / `reasoning_content`（`thought:true`）/ `tool_calls`；
`finishReason` 映射（STOP→stop、MAX_TOKENS→length、SAFETY 等→content_filter、有 tool_calls→tool_calls）；
`usageMetadata` → usage（thoughts 计入 completion，另记 reasoning_tokens，cached 字段缺失记 0）；
`promptFeedback.blockReason` → content_filter + WARN 日志。

**验收证据**：假上游样例（正常文本 / 工具调用 / blockReason / usage 缺失）断言产出的
`choices[0].message`、`finish_reason`、`usage` 三处；`token_tracker` 记账断言一次。

**依赖**：T1。**预估**：~70 行 + 断言。

## T3 流式转换

**目标**：`:streamGenerateContent?alt=sse` → OpenAI `chat.completion.chunk` 流。

**文件与位置**：`api_pool_server.py` `stream_generator` 内
- `_is_business_chunk` / `_business_activity` 各加 Gemini 判定（现有 `~5670` 区）
- chunk 映射分支（现 `~5903` 的 `else: yield line` 之前加 `elif is_gemini:`）
- 流末收尾（现 `~5922` 的 responses/anthropic 收尾旁）：补 finish 帧 + usage 帧 + `data: [DONE]`

**要点**：`text`→`content`、`thought:true`→`reasoning_content`、`functionCall`→`tool_calls` delta（整段参数）、
`finishReason`、`usageMetadata` 写入既有 `final_*` 变量（`finally` 里的记账自动生效）；
上游 `{"error": …}` 走既有流内错误分支。

**验收证据**：假上游 SSE 断言：客户端收到的 chunk 序列（content/reasoning/tool_calls 各一次）、
末帧 finish + usage、以 `[DONE]` 收尾；停滞判定在只发 usage 的收尾帧上不误判为无业务数据。

**依赖**：T1。**预估**：~80 行 + 断言。

## T4 前端协议下拉项

**目标**：GUI 能选/显示 `gemini` 协议。

**文件与位置**：`static/index.html`（协议 `<select id="fProtocol">`，现 `~615`；徽标渲染 `~769`）。

**验收证据**：静态断言页面含 `value="gemini"` 选项；`fProtocol` 取值链路（保存/回填）不变。

**依赖**：无（与 T2/T3 并行）。**预估**：1–2 行（徽标可选）。

## T5 离线假上游单测

**目标**：不碰网络就能证明转换正确。

**文件与位置**：新增 `test/test_gemini_bridge.py`（沿用 `test/` 现有脚本式断言风格，无框架依赖）。

**覆盖**：非流式文本；流式文本；工具调用 → 第二轮续跑（断言签名被附回 `functionCall`）；
无签名历史 → 文本降级且不产生 400；`blockReason`；usage 记账；`_gemini_url` 三种 base。

**验收证据**：`python3 test/test_gemini_bridge.py` 全绿并打印每条断言名。

**依赖**：T1、T2、T3。**预估**：~1 个文件。

## T6 本地端到端验证（真实上游）

**目标**：真实 soleapi 上跑通，且不动生产。

**做法**：宿主机 `/tmp/apipool-gemini-e2e-<ts>/` 复制工作区代码 + 只含 `Soleapi-gemini` 的最小
`api_config.json`（`protocol="gemini"`，独立端口如 5399，独立 DB 目录），运行后打：
非流式问答、流式问答、双轮工具续跑、图片（data URL）各一次。

**验收证据**：四条请求的真实响应内容（内容非空、`finish_reason` 正确、工具轮拿到 `tool_calls` 并在
第二轮得到最终答案、图片被正确识别）；日志中无 `missing thought_signature` 400；临时实例退出后清理。

**依赖**：T5。**预估**：~30 分钟实测。

## T7 部署 + 端点切换 + 灰度（**需批准**）

**目标**：生产可用。

**做法**：备份生产文件（带时间戳）→ 部署代码 → `Soleapi-gemini`（现 `openai`）与
`Soleapi-gemini-3.7-flash`（现 `responses`）`protocol` 改 `gemini` → 重启 `api-pool2` 服务 →
该端点先留独立组（`in_pool=false`，独立 group）观察，再并入 main。

**验收证据**：端点 `health` 由 `bad` 转好；生产日志中该端点请求成功、无签名类 400；
`token_stats` 有记账；连续 N 次真实请求（含工具轮）通过。

**依赖**：T6 + 用户明确批准。**回滚**：`protocol` 改回原值（或回滚文件 + 重启）。

---

## 完成定义（DoD）

1. `protocol="gemini"` 端点可用：非流式、流式、工具续跑、图片四条链路实测通过。
2. 其他协议端点行为无变化（回归：现有 `test/` 全跑一遍 + 生产观察 main 组无异常）。
3. 无签名场景不产生 400（降级为文本 + WARN 日志），且日志可观测。
4. 前端可选/可显示 `gemini`。
5. 文档同步：`PROJECT.md` 记录协议矩阵从 3 条扩到 4 条；本计划与设计文档路径互相引用。

## 明确不做（本轮）

入站 Gemini 方言、原生 Google 端点、`fetch-models` 的 `/v1beta` 适配、thinking 参数映射、
`n>1`、presence/frequency penalty、音频 parts、远端图片代抓、签名跨重启持久化、
非 Hermes 副本（`hooks/*.js`、`.openclaw/`）。
