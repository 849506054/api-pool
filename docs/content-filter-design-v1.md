# API Pool 2.0 敏感字过滤设计 v1

> 状态：**方案已确认，尚未实现。** 2026-08-24 定稿。
>
> 目标：在 API Pool 入口统一清洗上游敏感字，消除「上游限制不透明 + 本地排查困难 + 持久污染反复复发」三类问题。

## 1. 结论

在 API Pool 接收 Hermes 请求后、进入现有 `chat()` 业务流程之前，执行一次**全局**敏感字清洗；清洗后的 payload 贯穿端点选择、协议转换、重试、故障转移与本地日志的全部下游环节。

三个核心决策：

| 议题 | 决策 |
|---|---|
| 全局 vs 端点配置 | **全局**。过滤是统一入口治理能力，不是端点适配能力 |
| 删除 vs 改写 | **同语义替换优先**，删除仅作兜底 |
| 词典可见性 | **API Pool 私有配置**，Hermes 不可读、不接收、不持久化 |

## 2. 驱动实例（脱敏）

首版范围以真实案例为基准，不做泛化设计。

### 2.1 案例定位

- 代码位置：`/opt/hermes/agent/tool_dispatch_helpers.py`，函数 `_maybe_wrap_untrusted()`
- 命中内容：Hermes 对高风险工具返回结果套 `<untrusted_tool_result>` 包装时，**包装内的固定安全提示语**中含有一个被上游拦截的复合词（本文档以 `<SENSITIVE_TOKEN>` 指代，不落原文）
- 触发条件：每次调用高风险工具（web / MCP 等）稳定复现

### 2.2 命中字段路径

```text
messages[].role = "tool"
messages[].content              # 字符串形态
messages[].content[].text       # 多模态列表形态
```

### 2.3 关键结论

敏感词来源是 **Hermes 自身生成的固定提示语**，而非用户输入、工具参数或工具 schema description。

因此首版范围必须覆盖 `messages[].content`，而不是 `tools[].function.description`。

### 2.4 现状与本方案的增量价值

当前依靠修改 Hermes 源码（把该词改为安全形态）规避，该补丁已登记在 `hermes-update-workflow`，**每次 Hermes 升级都会被覆盖并需重新施加**。

改用 API Pool 入口过滤后：

- 不再修改 Hermes 源码
- 升级不丢失
- 新增敏感词只改配置，不改代码
- Hermes 侧该条补丁登记可退役

## 3. 过滤位置

```text
Hermes 请求
    ↓
API Pool HTTP 接收
    ↓
解析 payload
    ↓
【全局敏感字清洗】← 唯一执行点
    ↓
现有 chat() 流程
    ↓
端点选择 / 协议转换 / 重试 / 故障转移
    ↓
上游端点
```

### 3.1 为什么在 `chat()` 之前

- `chat()`（`api_pool_server.py` 约 L1504）负责端点选择与故障切换
- `_try_endpoint()`（约 L1810）负责单端点请求，一次请求可能被调用多次

若把过滤放在 `_try_endpoint()` 内：

1. 端点轮转与内部重试会重复扫描同一份文本
2. 替换结果可能被二次改写，产生不可逆的叠加损坏
3. 无法保证各端点收到的 payload 一致

### 3.2 日志脱敏不作为要求（2026-08-24 修正）

**原设计要求「过滤必须早于日志提取、日志只存清洗后内容」，该要求已取消。**

理由：只要 Hermes 的出站流量统一经过 API Pool，日志中是否保留原文都不影响上游。即使运维读取 `chat_logs.db` 使原文重新进入 Hermes 上下文，下一轮出站请求仍会在入口被清洗，上游不会收到敏感字。

因此本方案：

- **不修改** `ChatLogger`
- **不修改** `extract_prompt_text()`
- **不需要**清理存量 `chat_logs.db` 历史记录

入口过滤点的选择理由改为：单次执行、所有端点收到一致 payload、下游零改动。

> 边界：该推论依赖「Hermes 出站统一经过 API Pool」。若将来存在绕过 API Pool 的直连路径，日志原文才会形成真实污染源。

### 3.3 免修改的下游

在入口完成清洗后，以下路径无需改动：

- 端点选择与优先级轮转
- `_try_endpoint()`
- Anthropic 协议转换
- 冷却 / defer / 故障转移
- 流式处理
- 重试逻辑

## 4. 过滤范围

### 4.1 首版处理（全局，不区分端点）

```text
messages[].content                      # 字符串
messages[].content[].text               # 多模态文本块
messages[].reasoning_content            # 推理文本（DeepSeek 等端点回传校验）
messages[].reasoning_text               # 同上变体
messages[].name                         # 消息级名称
tool_calls[].function.arguments         # 工具调用参数（JSON 字符串/对象，仅替换值不碰 key）
```

覆盖全部 role：`system` / `user` / `assistant` / `tool`。

预防性一并覆盖（纯说明文本，零执行语义）：

```text
tools[].function.description
tools[].function.parameters..*.description
```

> 注：`tools[].*.description` **不是**本次实例所在位置，属于预防性覆盖。

### 4.1.1 范围扩展依据（2026-08-24）

首版确认后，根据当前会话真实请求结构补充了三个位置：

- **`tool_calls[].function.arguments`**：真实会话中敏感词会随工具调用参数进入历史消息（例如配置写入、代码读取的参数值）。实现上优先 `json.loads` 解析后递归替换字符串值（JSON key 不触碰），解析失败则直接对字符串做替换，保证不破坏 JSON 结构。
- **`reasoning_content` / `reasoning_text`**：DeepSeek 等端点要求回传推理文本，若其中含敏感词会触发上游拦截。
- **`messages[].name`**：消息级名称字段，低概率但顺带覆盖。

### 4.2 首版不处理

```text
tools[].function.name
tool_call_id
role=tool 的 tool_call_id 绑定关系
URL / 文件路径 / ID / 枚举值
JSON key（arguments 中的 key 只替换 value）
正则表达式、代码块、结构化约束字段
```

原因：这些是**执行数据**而非 Prompt 说明，改写会导致工具调用失败、搜索词漂移、路径失效或 JSON 语义变化。`tool_calls[].function.arguments` 已纳入处理（§4.1.1），但仅替换字符串值、不碰 key，且默认走同语义替换以保持执行语义。

### 4.3 不参与过滤的请求

- API Pool 管理接口（`/api/*`）
- 健康探活请求（`_check_one_health` / `_probe_endpoint` 的 ping payload）
- 模型列表请求（`fetch_models`）

## 5. 词典设计

### 5.1 格式

采用 `key=value` 语义映射，**替换优先于删除**：

```text
# 同语义替换（推荐）
<敏感词>=<等义安全词>

# 删除（兜底），值为一个空格
<敏感词>=" "
```

本实例采用同语义替换：将下划线形态的复合词替换为空格形态，模型读到的含义完全不变。

### 5.2 为什么替换优于删除

本实例的敏感词位于 Hermes 的 **prompt injection 防护提示语** 中。直接删除会破坏该句可读性，等于削弱安全边界。同语义替换保留原意。

### 5.3 实现约束

1. **单次扫描**：词典加载时编译为匹配器（正则或 Aho-Corasick），常驻内存，词典变更才重新编译
2. **长词优先**：避免短词先命中截断长词
3. **替换结果不参与二次匹配**：防止 A→B、B→C 链式改写
4. **切分规则**：`key=value` 只按**首个** `=` 切分（词本身可能含 `=`）；值两侧引号可选剥离
5. **结构保护**：不得改动 `<untrusted_tool_result>` 等结构性标签本身
6. **快速路径**：未启用或词典为空时直接跳过，不做深拷贝

## 6. 配置边界（硬约束）

### 6.1 词典存放

```text
API Pool 私有运行目录
├── api_config.json          # 现有端点配置
└── content_filter.json      # 新增，仅 API Pool 进程读取
```

文件权限限制为服务运行用户可读。

### 6.2 禁止路径

词典及命中原文**不得**出现在：

- Hermes `config.yaml` / provider 配置 / plugin 配置
- Hermes `extra_body`、system prompt、请求参数
- API Pool 请求头、请求体
- API Pool 错误响应
- API Pool 运行日志、chat 日志
- `/v1/models`、状态接口、任何管理接口返回值
- 前端页面

### 6.3 允许暴露的元信息

管理接口与前端只允许返回不含原文的摘要：

```json
{
  "enabled": true,
  "profile": "default",
  "dictionary_version": "2026-08-24",
  "word_count": 128
}
```

明确禁止：

```json
{ "dictionary": ["...原文..."] }
```

## 7. 持久化策略

### 7.1 入口清洗已覆盖持久化链路的污染风险

API Pool 内部所有持久化与可观测通道（`chat_logs.db`、运维日志、错误日志、重试日志、前端日志接口）**不强制脱敏**。

依据 §3.2：出站统一经 API Pool 时，日志原文即使被读回，下一轮请求仍会在入口被清洗。

### 7.2 过滤器自身不落原文

过滤功能自身的日志与统计**不得**打印敏感词原文，这是过滤器实现约束而非持久化要求：

```json
{
  "filtered": true,
  "matched_count": 2,
  "filter_duration_ms": 0.4,
  "dictionary_version": "2026-08-24"
}
```

如需定位命中位置，只记录**字段路径**，不记录文本：

```text
messages[7].content
tools[0].function.description
```

### 7.3 明确不做

- 不保存清洗前后对照文本
- 过滤器日志不打印命中原词
- 不把过滤指标通过模型响应回传给 Hermes
- 不修改 `ChatLogger` / `extract_prompt_text`
- 不清理存量 `chat_logs.db` 历史记录

## 8. 失败策略

词典加载失败、格式错误或过滤器初始化异常时：

```text
拒绝请求 → 不发送原始 payload → 不记录原文 → 返回通用错误
```

对 Hermes 只返回：

```json
{
  "error": {
    "message": "API Pool content filter unavailable",
    "type": "service_unavailable"
  }
}
```

**禁止为保持可用性而降级放行原始请求**——否则过滤器故障时正好构成绕过路径。

## 9. 性能

入口单次清洗的性能特征：

- 每请求仅执行一次，不随端点数量增长
- 不随重试次数重复执行
- 匹配器常驻内存，复杂度约 `O(文本总长 + 命中数)`
- 空词典 / 关闭状态走快速路径

需关注的边界：大上下文、高并发、大型工具 JSON。建议为单请求设置最大过滤文本长度上限；过滤耗时超阈值时只记录耗时，不打印内容。

## 10. 配置模型

全局配置：

```json
{
  "content_filter": {
    "enabled": true,
    "dictionary_version": "2026-08-24",
    "targets": [
      "messages.content",
      "messages.text_blocks",
      "tools.descriptions"
    ],
    "max_filter_chars": 0,
    "dictionary": {
      "<敏感词>": "<等义安全词>"
    }
  }
}
```

`max_filter_chars` 为 `0` 表示不限。

第一版不引入端点级覆盖字段——过滤是统一治理能力，拆到端点会重新分散策略。若后续确有请求级例外需求，再增加全局例外规则。

## 11. 首版范围

### 包含

- 全局开关与私有词典文件
- `key=value` 语义映射，替换优先
- 入口单次清洗，贯穿全链路
- 覆盖 `messages[].content` 及多模态 text（全 role）
- 覆盖 `reasoning_content` / `reasoning_text` / `messages[].name`
- 覆盖 `tool_calls[].function.arguments`（JSON 值，不碰 key）
- 预防性覆盖 `tools[].*.description`
- 命中次数 / 耗时 / 字段路径统计
- 过滤器异常时拒绝请求
- 单元测试：字符串 content、多模态 text、tool role、空词典、长词优先、替换不二次匹配、结构标签保护、arguments JSON 值、reasoning 字段、消息 name

### 不包含

- 自动识别敏感词
- 语义级智能改写
- 响应内容过滤
- 在线词典同步
- 端点级策略与 UI 编排
- 日志脱敏 / `ChatLogger` 改造
- 历史 `chat_logs.db` 存量清理

## 12. 已知边界

1. 本方案依赖「Hermes 出站统一经过 API Pool」；若存在绕过 API Pool 的直连路径，该路径上的原始请求与日志原文会形成污染源。
2. 本轮只确认了一处敏感词位置（§2），未穷尽排查其他可能位置。
3. 「该词触发上游拦截」这一因果依据现有本地补丁的存在及上游使用日志报错，本轮未做隔离实测复现。
