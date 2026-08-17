# API Pool 2.0 最小范围设计稿：非 DeepSeek 端点 fallback

> 状态：设计稿 v1，2026-08-18。仅定义方案，尚未实现、部署或切换 Hermes 流量。
>
> 唯一目标：在 API Pool 1.0 已有端点池、priority、重试、冷却、轮转和 fallback 基础上，使 API Pool 可以安全切换到非 DeepSeek API 端点，并保持 Hermes 侧会话连续。

## 1. 原始需求与原则

API Pool 当前基本是 DeepSeek API 专属工具。2.0 只解决一个问题：**让现有 1.0 fallback 池能够承载非 DeepSeek 端点**。

实现方式参考 Hermes 原生 fallback：一次请求失败时，在同一份完整上下文上切换到另一个已配置的“provider/model”目标；下一轮仍由 Hermes 持有和发送会话历史。

API Pool 不新增自己的会话状态，不复制 Hermes agent fallback 状态机，也不自动判断任务应该使用哪个模型。

## 2. 保留 1.0，不重构控制流

以下结构和行为保持不变：

- `Endpoint` 仍是池内最小配置单元，继续包含 `base_url`、凭据、`model`、`protocol`、headers、priority、timeout、重试和冷却配置。
- `_active_endpoints()` 继续筛选可用端点。
- `chat()` 继续按 priority 选择端点、执行轮转和恢复。
- `_try_endpoint()` 继续负责单个端点请求及现有协议处理。
- `_rotate()`、cooldown、defer、终极兜底和 `AllEndpointsFailed` 继续复用。
- Hermes 继续只配置一个稳定的 API Pool OpenAI-compatible 地址。

不引入 `ModelRoute` 子实体、route 级健康状态、独立 fallback 图、模型能力库或新的全局路由器。

## 3. 最小功能改动

### 3.1 端点仍然一端点一模型

每个 Endpoint 继续明确声明它自己的：

```json
{
  "name": "non-deepseek-endpoint",
  "base_url": "https://provider.example/v1",
  "model": "gpt-5.6-sol",
  "protocol": "openai",
  "priority": 2,
  "in_pool": true
}
```

因此跨模型切换通过现有端点轮转自然实现：

```text
Endpoint A: deepseek-v4-flash  -> 失败
Endpoint B: gpt-5.6-sol        -> 继续请求
Endpoint C: claude-opus-5      -> 继续请求
```

不为同一个上游凭据复制多个伪端点。只有在当前 1.0 数据模型确实要求“一端点一模型”时，才允许以现有端点形式登记测试目标；这属于现有结构的暂时兼容，不新增第二套实体模型。

### 3.2 请求模型必须跟随目标端点

当前 `chat()` 已经使用 `ep_model = model or ep.model` 组装请求。2.0 必须保持并验证这一点：每次轮转到新 Endpoint，都用该 Endpoint 的 `ep.model`，不能把第一次请求的模型名继续带给后续端点。

API 入口需保留上游请求中的 model 仅用于兼容检查或日志，不得让它破坏 API Pool 现有的端点模型选择。

### 3.3 DeepSeek 专属处理必须隔离

现有 DeepSeek 兼容逻辑只能对满足 DeepSeek 条件的 Endpoint 生效：

- `reasoning_content` / `reasoning_text` 的补全或缓存注入。
- DeepSeek thinking 相关字段处理。
- `tool_call_id_prefix` 重写，例如 `call_00_ET_`。
- 任何针对 DeepSeek 400 错误的字段修复。

非 DeepSeek Endpoint 必须保持原始消息和目标协议允许的字段，不得注入 DeepSeek 专属字段。

隔离判断应优先使用端点的明确配置或协议能力，不凭当前主模型名称猜测整个请求链。没有明确能力信息时，默认 passthrough，不注入 DeepSeek 字段。

### 3.4 协议切换沿用现有 Endpoint 配置

2.0 只确保轮转时使用目标 Endpoint 已有的 `protocol`、`base_url`、headers 和 model。现有 OpenAI-compatible / Anthropic 分支继续复用，不在本版本统一重写 reasoning 协议。

若非 DeepSeek 端点需要特殊请求转换，必须先作为该 Endpoint 已有 `protocol` 能力的一部分验证；不能借 2.0 顺便设计通用协议适配层。

## 4. fallback 语义

### 4.1 单请求内

1. API Pool 按现有 priority 选择第一个可用 Endpoint。
2. 上游失败时沿用 `_try_endpoint()` 重试。
3. 重试耗尽后沿用 `_rotate()` 冷却并轮转到下一个可用 Endpoint。
4. 新 Endpoint 使用自己的 model、protocol 和 headers 重新发送同一份请求上下文。
5. 所有 Endpoint 失败后仍抛出 `AllEndpointsFailed`，由 Hermes 按自身 fallback 规则处理。

这与 Hermes 原生 fallback 的兼容点是：模型/端点切换发生在同一轮请求内，Hermes 的消息历史不丢失；但 API Pool 不复制 Hermes 的更高层 fallback 状态管理。

### 4.2 流式请求

沿用 1.0 的流式策略：只有在尚未向 Hermes 输出有效上游内容时，才允许轮转到下一个 Endpoint。已经开始输出的流不可拼接另一个模型的输出。

本条只作为现有流式 fallback 的兼容验收要求，不新增全量缓冲或新的流式状态机。

## 5. 最小验收矩阵

### 5.1 回归

- 全部 DeepSeek Endpoint：现有 chat、stream、tools、thinking 行为不变。
- 1.0 单端点、priority、重试、cooldown、defer 和终极兜底行为不变。
- Hermes 仍可通过同一 API Pool 地址正常完成多轮工具调用。

### 5.2 非 DeepSeek 切换

- DeepSeek Endpoint 失败后切换到 OpenAI-compatible 非 DeepSeek Endpoint。
- DeepSeek Endpoint 失败后切换到 Anthropic protocol Endpoint（如已配置并验证）。
- 切换目标使用目标 Endpoint 自己的 model 和协议配置。
- 非 DeepSeek 请求不含 DeepSeek 专属注入字段。
- 非 DeepSeek 成功响应能被 Hermes 正常解析。
- 非流式和流式请求分别验证。
- tools 请求验证 assistant/tool 消息配对、tool_call id 和响应结构。
- 所有候选失败后仍返回既有 `AllEndpointsFailed`，不新增特殊错误体系。

## 6. 明确不做

- 不实现 `ModelRoute`、模型能力库或独立 Upstream 实体。
- 不实现主动选模、任务复杂度分析、成本感知调度。
- 不实现复杂的显式 fallback chain 或递归 fallback 图。
- 不实现 route 级独立健康/冷却状态。
- 不重构 `APIPool.chat()` 主循环、`_try_endpoint()` 请求框架或 `_rotate()` 故障转移机制。
- 不重构日志数据库、UI 路由编辑器或模型管理页面。
- 不在本版本统一 DeepSeek/OpenAI/Anthropic reasoning wire 字段。
- 不修改 Hermes agent loop、`/model` 或 Hermes fallback 配置。
- 不自动切换生产 Hermes 到 5200；切流必须另行评审。

## 7. 实施顺序

1. 对照 1.0 代码定位所有 DeepSeek 专属假设，并给出最小隔离修改清单。
2. 只修改必要的兼容判断和目标 Endpoint 请求构造。
3. 用临时测试端点验证 OpenAI-compatible 与 Anthropic 两类非 DeepSeek 目标。
4. 先运行 ruff/py_compile 和针对性测试，再做 2.0 实例冒烟。
5. 在 5200 实例完成真实切换矩阵后，单独提交是否切换 Hermes 的决策。

本设计的成功标准不是新增功能数量，而是：**1.0 行为保持不变，同时失败时可以切换到非 DeepSeek Endpoint，并且 Hermes 无需改变接入方式。**
