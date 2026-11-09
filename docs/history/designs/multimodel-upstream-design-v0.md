# API Pool 2.0 多模型上游建模 — 设计终稿

> 状态：**历史方案，已废弃，不得作为实现依据**（2026-08-18）。本文档曾把 API Pool 2.0 扩张为多模型路由重构；当前唯一有效设计是 `docs/minimal-non-deepseek-fallback-design-v1.md`，只在 1.0 fallback 基础上增加非 DeepSeek 端点兼容。

## 1. 背景与病灶（观察实证）

1. `Endpoint` dataclass（api_pool_server.py L407-433）把「上游连接」与「单个固定 model」耦合一体。
2. `/v1/chat/completions` 入口（L1904）把 `model` 从请求体**剥离**，`chat()` 用 `ep_model = model or ep.model`（L1237）→ 对外 `body.model` 从不参与路由，实际总用端点的固定 model。
3. `api_config.json` 中 `AgentRouter`(ps.air-outer.com) 仅注册 `claude-opus-5` 一个 model，但上游实际提供多模型（claude-opus-5 / gpt-5.6-sol …）。这是「复制同一凭据造多个伪端点」的温床。
4. `fetch_models()`（L1798）已可 GET `/models` 拿真实目录（id/pricing/description/modality），但只做 UI 展示，未落地数据模型、未参与路由。
5. `_active_endpoints()` + priority 轮转按**端点**维度选，非按 model。

## 2. 实体划分（用户已拍板）

- **保留 `Endpoint` = 上游连接单元**：base_url / 凭据 / headers / use_proxy / 健康 / 冷却 / priority / defer 等原样不动。
- **新增可选能力字段 `model_routes: list[ModelRoute]`**，挂在 Endpoint 上（一个连接 = 一个 Endpoint + N 个 routes）。
- **兼容规则**：Endpoint 未配 `model_routes` → 完全退化为现有单模型旧语义（`endpoint.model`），存量 `api_config.json` 零改动、默认关闭不改变现有运行行为。

## 3. 数据模型

### 3.1 ModelRoute（新增子结构）

```json
{
  "model": "gpt-5.6-sol",
  "reasoning_protocol": "passthrough",   // passthrough|deepseek|openai|anthropic|none；默认 passthrough，零改写
  "vision": false,
  "max_context_k": 0,                     // 0=不限；超限跳过该 route（复用 1.0 max_context_k 语义，但不冻结）
  "enabled": true,
  "priority": 0                           // 同连接内多 model 相对优先；跨连接仍由端点 priority 主导
}
```

### 3.2 Endpoint 增量

- 新增字段 `model_routes: list[ModelRoute] = field(default_factory=list)`（请求侧 `_adapter` 白名单需同步）。
- 运行态 `_current_model` 缓存最近/当前命中 model（不影响现有 `_current_endpoint_id` 语义，仅新路由用）。

## 4. 路由语义（`/v1/chat/completions`）

### 4.1 入口改动
- 不再剥掉 `model`：`pool.chat` 收到显式 `body.model` 时进入**模型泳道匹配**；否则走旧路径。

### 4.2 匹配链（优先级由高到低）
1. **精确路由**：在池内所有 Endpoint 的 `model_routes`（含单模型信息）中找 `model == body.model` 的候选，组装为 `(Endpoint, ModelRoute)` 运行单元列表。
2. 候选按「端点 priority 为主、route.priority 为次」排序。
3. 对候选单元复用 1.0 `_try_endpoint` 轮转 / 冷却 / cooldown / defer / 终极兜底（**用户拍板：复用语义**），全部失败才抛 `AllEndpointsFailed`。

### 4.3 未匹配兜底（用户已拍板：方案 A）
- 若 `body.model` 找不到任何匹配的 route/单模型 → **回退「仅按端点 priority 轮转」旧行为**（最大向后兼容）。
- 已知后果（已向用户明示并接受）：请求 `claude-opus-5` 而池只有 deepseek 路由时，模型串会落达上游可能被 400 拒。设计上保留，不用硬 400。

## 5. reasoning 意图适配衔接（分阶段，默认 passthrough）

- 本次多模型建模仅引入 `reasoning_protocol` 字段（默认 passthrough），**不做** wire 改写（避免升级即改变行为）。
- 「统一 reasoning 意图 → 端点级 wire 转换」单独立 tick，遵循 `references/hermes-unified-reasoning-endpoint-adaptation.md`：在「目标端点已选定、调用 `_try_endpoint` 前」适配；每次端点多尝试从原始请求构造独立 payload，禁止原地删除字段污染下一端点。

## 6. UI / API / 持久化

- UI：Endpoint 详情加「模型路由」可折叠面板（列表 + 增删改 + 批量从 `fetch_models` 拉取导入）。
- API：`/api/fetch-models` 结果可一键「导入为 model_routes」（自动推断 reasoning_protocol/vision/max_context_k 默认值）。
- `_sync_to_config()` 显式白名单必须加 `model_routes`；`GET/POST /api/endpoints` 序列化同步覆盖。

## 7. 测试矩阵（DeepSeek/非 DeepSeek）

- 同一上游多模型：DeepSeek 模型 + 非 DeepSeek 推理模型 + 非推理模型，各验证 chat/stream/tools。
- 跨上游、跨模型协议故障转移：候选 route 全失败 → 正确报错，不污染消息历史 reasoning/tool_call。
- 未匹配兜底（方案 A）：请求不存在模型 → 回退端点轮转，验证行为与旧版本一致。
- 存量端点无 `model_routes` → 行为零变化回归。
- 流式 + 非流式同一适配函数（与 reasoning_tick 对齐）。

## 8. 迁移顺序（安全）

1. 实现数据模型层（`ModelRoute` dataclass + `Endpoint.model_routes` + 白名单），默认全覆盖 passthrough。
2. UI + API 只读展示 → 再写。
3. `fetch_models` 批量导入流程。
4. 流式/非流式请求路径联调。
5. 用 ps.air-outer.com(AgentRouter) 真实多模型验证。
6. 全部通过后才考虑 Hermes 侧 provider 切换指向 5200。

## 9. 不做（明确范围外）

- 本 tick **不做** reasoning wire 改写、`/v1/responses`、独立第二实体 `ModelRoute` 表、Hermes 侧切流。均列后续。