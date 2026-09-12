# Gemini 兼容协议适配方案（2026-09-12，待批，未写代码）

一句话：给 API Pool 增加端点级协议枚举 `gemini`，出站改走 Gemini 原生方言
（`/v1beta/models/{model}:generateContent`），把 `Soleapi-gemini`、
`Soleapi-gemini-3.7-flash` 两个端点的 `protocol` 由 `openai`/`responses` 改成 `gemini`。

状态：**仅方案**。工作区 `/opt/data/work/api-pool2/api_pool_server.py` 与生产逐字节一致
（md5 `db05802e…`），此前那段未接线的草稿已回滚，存为
`/opt/data/backups/gemini-bridge-draft-20260912.patch`（327 行，批准后可作起点）。

---

## 1. 问题与根因（全部为实测证据）

症状：`Soleapi-gemini` 端点 `health=bad`，`health_error` =
`HTTP 404: 当前没有可提供模型 gemini-3.8-flash 的货源…`。

同一把 Key、同一模型，按入口协议逐个打（UA/鉴权按 pool 现有出站形态）：

- `POST /v1/chat/completions` → **404** `no_endpoint_for_format`「当前没有能承接该入口协议的货源」
- `POST /v1/responses` → **404** 同上
- `POST /v1/messages` → **404** 同上
- `POST /v1beta/models/gemini-3.8-flash:generateContent` → **200**，正常内容
- `POST /v1beta/models/gemini-3.8-flash:streamGenerateContent?alt=sse` → **200**，正常 SSE
- `POST /v1beta/models/gemini-3.7-flash:generateContent` → **200**

结论：soleapi 侧 Gemini 系模型**只开原生方言**，OpenAI/Responses/Anthropic 三个入口对它没有货源。
官方文档佐证：错误码表 `model_rejects_format` / `no_endpoint_for_format` =「该模型不接受当前入口协议」。

其他实测结论（决定实现细节）：

- **鉴权**：`Authorization: Bearer`、`x-goog-api-key`、两者同时 → 均 200。
- **工具调用**：`functionCall` 可用；**`thoughtSignature` 必须回传**——历史里 functionCall part 不带签名，
  续跑必 400 `Function call is missing a thought_signature`（`thinkingBudget=0` 也一样，
  实测签名长 528 字符）。仅文本响应的签名可忽略。
- **图片**：`inlineData`（data URL）可被接受并正确识图。
- **其他可用键**：`generationConfig`（temperature/topP/maxOutputTokens/stopSequences/responseMimeType）、
  `systemInstruction`、`toolConfig.functionCallingConfig`。
- **响应字段**：`candidates[0].content.parts[]`（`text` / `thought:true` 思考文本 / `functionCall` /
  `thoughtSignature`）、`finishReason`、`usageMetadata`（promptTokenCount / candidatesTokenCount /
  thoughtsTokenCount / cachedContentTokenCount / totalTokenCount）、`promptFeedback.blockReason`。

---

## 2. 设计（最小改动）

- 端点级新增协议枚举值 `gemini`，与现有 `openai` / `responses` / `anthropic` 并列。
- **入站不变**：对外仍是 OpenAI chat/completions 与 `/v1/responses`（后者内部已归一到 chat payload）。
- **出站转 Gemini 原生**；响应再转回 OpenAI 形态（非流式 chat.completion / 流式 delta）。
- tool_call id 由 pool 生成（`call_<hex20>`），上游签名按该 id 记在进程内表（TTL 1h、上限 4000 条）。
- 续跑时把签名附在 `functionCall` part 上回传。
- **无签名时降级**：该 assistant 轮的 functionCall 与之对应的工具结果一起退化成文本 parts，
  让对话继续（而不是硬 400 → 轮转）。触发场景：历史由其他协议的端点产生，或 pool 重启后失忆。

---

## 3. 逐处改动点（文件 + 位置 + 内容）

### 3.1 `api_pool_server.py` — 新增 Gemini 桥接 helper（约 320 行，插在 `class Endpoint` 之前）

`_gemini_url`（base_url 支持 `…/v1`、`…/v1beta`、裸域名三种写法）、
`_gemini_payload_from_chat`（OpenAI chat payload → Gemini 请求体）、
`_gemini_parts_from_content` / `_gemini_text_from_content`（content block、图片 data URL → inlineData）、
`_gemini_tools_from_chat` / `_gemini_tool_config_from_chat`、
`_gemini_parts_to_chat`（parts → text/reasoning/tool_calls）、
`_gemini_finish_reason`、`_gemini_usage_to_chat_usage`、
`_gemini_remember_tool_call` / `_gemini_lookup_tool_call`（签名表）。

### 3.2 `_try_endpoint` 请求构造（现有 `~5280` 与 `~5397` 两处）

- 增加 `is_gemini` 判定与分支：URL = `{归一化 base}/models/{ep.model}:generateContent[?alt=sse]`，
  body = `_gemini_payload_from_chat(payload, ep)`。
- 鉴权：保留既有 `Authorization: Bearer`，追加 `x-goog-api-key`。
- 末尾 `elif not is_responses:` 改为 `elif not (is_responses or is_gemini):`。

### 3.3 出站头白名单（`_PROFILE_RESERVED_HEADERS`）

加入 `x-goog-api-key`：伪装 profile 不能覆盖它，透传时也不会把客户端的 Google Key 带上去。

### 3.4 流式路径（`stream_generator` 内）

- `_is_business_chunk` / `_business_activity` 各加 Gemini 判定（首包判定、停滞检测用）。
- chunk 映射：`text`→`content` delta、`thought:true`→`reasoning_content`、
  `functionCall`→`tool_calls` delta（整段参数一次给全）、`finishReason`、`usageMetadata`。
- 流末补齐 finish 帧 + usage 帧 + `data: [DONE]`（Gemini SSE 没有 `[DONE]`，不补下游会等不到结束标记）。

### 3.5 非流式路径（现有 `~5963`）

新增 Gemini 分支，产出标准 OpenAI `chat.completion`：`message.content` /
`reasoning_content` / `tool_calls`、`finish_reason`（STOP→stop、MAX_TOKENS→length、
SAFETY 等→content_filter、有 tool_calls→tool_calls）、`usage`。
`promptFeedback.blockReason` → `content_filter` + WARN。沿用既有 `check_fake_success`、
`token_tracker`、`chat_logger` 记账。

### 3.6 前端 `static/index.html`

协议下拉增加 `<option value="gemini">Gemini 兼容</option>`（1 行）。端点徽标可加一个颜色标签（可选）。

### 3.7 配置

`Soleapi-gemini`（现 `openai`）、`Soleapi-gemini-3.7-flash`（现 `responses`）的 `protocol` 改为 `gemini`
（配置改动，随部署一起做，不单独提前改）。

---

## 4. 影响面

- **只影响 `protocol="gemini"` 的端点**；openai / responses / anthropic 三条路径零改动（分支按协议前置判断，diff 隔离）。
- 新增一处全局状态：签名表 dict + 锁，容量上限 4000 条（几十 KB 级）。
- 新增一个被 pool 托管的出站头（`x-goog-api-key`）。
- 探活（`health_mode=chat`）自动走新路径；`fetch-models` 不用动——soleapi 的 `/v1/models`
  用 `Authorization` 即可返回完整模型列表。
- 前端只多一个下拉项。

---

## 5. 明确不做（本次不做，需要就单开）

- 入站 Gemini 方言（对外暴露 `/v1beta/models/…`）：入站继续只收 OpenAI 形态。
- 原生 Google 端点（`generativelanguage.googleapis.com`）与 `fetch-models` 的 `/v1beta` 适配。
- thinking 参数映射（`reasoning_effort` → `thinkingBudget`/`thinkingLevel`）、`n>1`、
  presence/frequency penalty、音频 parts。
- 远端图片 URL 代抓（降级为 `[Image URL: …]` 占位文本，与现有 Anthropic 桥一致）。
- 签名跨重启持久化（先做进程内；不够再加 sqlite）。
- `hooks/*.js`、`.openclaw/` 等非 Hermes 路径的副本。

---

## 6. 风险与缓解

- **跨端点历史无签名**（同一条会话历史里混有 OpenAI 协议端点生成的 tool_call）→ 工具轮退化为文本，
  有 WARN 日志。缓解：gemini 端点可先放专用组，或后续做签名持久化。
- **pool 重启后失忆** → 同上，退化为文本，不会 400。
- Gemini 不返回缓存字段是常态 → `cached_tokens` 记 0。
- 200 但空内容/被拦截 → 靠 `promptFeedback.blockReason` 与既有 `check_fake_success` 判定，不新造机制。
- 不同 Gemini 模型对签名/思考字段要求不一 → 以 `gemini-3.8-flash`、`gemini-3.7-flash` 实测为准，
  其他模型首次启用前单独验证。

---

## 7. 验收方式（部署前全部在离线/本地完成）

1. **离线单测**：起一个本地假上游（HTTP server 模拟 soleapi 的 `/v1beta` 方言），覆盖
   非流式文本、流式文本、工具调用→续跑（签名回填）、无签名降级、blockReason、usage 记账；
   放进 `test/`，运行方式与现有 test 一致。
2. **本地端到端**：宿主机 `/tmp` 独立目录 + 独立端口（如 5399）+ 只含 `Soleapi-gemini` 的最小配置，
   打真实 soleapi：非流式、流式、双轮工具续跑、图片各一次。
3. **灰度**：部署后该端点先留在独立组，观察日志——请求成功、无 `missing thought_signature` 400、
   usage 有正常记账——再并入 main。
4. **回滚**：端点 `protocol` 改回 `openai`/`responses` 即回到当前行为；代码是纯新增分支，
   不启用该协议时零影响。

---

## 8. 落地顺序与工作量

顺序：helper → 请求侧 → 非流式 → 流式 → 前端 → 单测 → 本地 E2E →（等你批）部署 + 灰度。

工作量：helper 约 320 行 + 五处接线约 120 行 + 前端 1 行 + 1 个测试文件。

---

## 9. 需要拍板的三件事（均给了默认值）

1. **图片支持**：默认做（data URL → `inlineData`；远端 URL → 占位文本）。
2. **无签名降级为文本**：默认接受（比 400 轮转更可用）。
3. **部署范围**：默认只改这两个端点 + 重启 `api-pool2` 服务；代码先在工作区完成，
   未经批准不动生产目录。
