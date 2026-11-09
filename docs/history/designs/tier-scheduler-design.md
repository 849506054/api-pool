# 🏗️ 模型能力层级调度 — 设计愿景

> 本文档记录 API Pool 未来的扩展方向，非当前实现。  
> 基于实际使用反馈整理，择机实现。

---

## 1. 背景

### 当前调度模型（已实现）

```
端点 (priority=1) ── 故障转移 ── 端点 (priority=2) ── ... ── 端点 (priority=N)
```

- 每个端点是一个"黑盒"，一个端点只对应一个模型
- priority 以端点为粒度，表示故障切换顺序
- 同一条请求永远走同一个端点模型

### 用户发现的问题

中转 API 端点（如 Kcne、x6m6x）背后往往是**多模型服务**——同一个端点可以提供 flash、pro、r1 等多个能力等级不同的模型。  
当前调度只能"端点级故障转移"，无法在一个端点内部根据请求复杂度选择合适等级的模型。

### 需求

```
端点 (priority=1) ── 端点内模型选择 ── flash (tier 1, 快/便宜)
                    ├── pro   (tier 2, 均衡)
                    └── r1    (tier 3, 强/贵)
端点 (priority=2) ── 同上一级结构（故障降级后仍可享受端点多模型能力）
```

- 简单问题 → 轻量模型（flash/mini/haiku），快、省
- 复杂推理/长文 → 自动升级到更强模型（pro/r1/opus）
- 端点故障后 → 同优先级级别的次优端点继续提供等价模型能力
- 兜底端点 → 只有复杂请求才触发

---

## 2. 核心数据：模型能力等级库

### 2.1 思路

建立一个独立于端点配置的**模型能力数据库**，维护业界主流模型的相对能力等级（tier）。  
端点配置中只需声明"我提供哪些模型"，系统自动查询能力库获取各模型的 tier。

**核心收益**：模型能力数据与端点配置解耦。新增端点时只需填模型名列表，自动获得 tier 信息。  
别名（如 A 站叫 `deepseek-chat`、B 站叫 `deepseek-flash`）通过一次记录即可覆盖所有配置了该模型的端点。

### 2.2 数据模型（三表结构）

```mermaid
erDiagram
    MODELS {
        string canonical_id PK "规范模型名"
        string provider    "厂商（DeepSeek / OpenAI / Anthropic）"
        int tier           "能力等级 1-4"
        bool reasoning     "是否支持推理链"
        string family      "模型族（v4-flash / v4-pro / r1 / sonnet）"
        float cost_unit    "相对成本（flash=1）"
    }

    ALIASES {
        int id PK
        string name        "端点暴露的模型名（deepseek-chat）"
        string canonical_id FK "指向 MODELS.canonical_id"
        string endpoint_id FK? "可选：限制仅对特定端点生效"
    }

    ENDPOINT_MODELS {
        int id PK
        string endpoint_id FK
        string alias_name  "该端点声明的模型名"
        string canonical_id FK
    }

    MODELS ||--o{ ALIASES : "canonical_id"
    MODELS ||--o{ ENDPOINT_MODELS : "canonical_id"
```

### 2.3 查询链路示例

```
请求: POST /v1/chat/completions { model: "deepseek-chat" }

1. ALIASES 查 "deepseek-chat"
   → canonical_id = "deepseek-v4-flash"

2. MODELS 查 "deepseek-v4-flash"
   → tier = 1, family = "v4-flash", reasoning = false

3. 调度决策:
   - 这是一个轻量模型请求
   - 从端点池中选 priority 最高的、提供该模型（或同 tier 模型）的活跃端点
   - 端点 Kcne 同时提供 flash(1) / pro(2) / r1(3)，命中
   - 请求落在 Kcne → deepseek-chat
```

### 2.4 数据维护方案

| 来源 | 覆盖度 | 维护方式 |
|:--|:--:|:--|
| 手搓常见模型核心表 | 90% 常用模型 | 一次写完，偶尔补 |
| 用户使用中发现的缺失模型 | 长尾 | PR/issue 补充 |

别名表几乎不需要维护：同一个端点的模型名是稳定的模板，大部分中转站使用标准名或可预测的变体。

---

## 3. 调度策略

### 3.1 层级结构

```
       ┌─────────────────────────────────────┐
       │           客户端请求                   │
       │    { model: "deepseek-chat" }        │
       └──────────────────┬──────────────────┘
                          ▼
       ┌─────────────────────────────────────┐
       │       第一层：端点优先级选择           │
       │   active.sort(key=endpoint.priority) │
       │   从最高优先级端点的模型中寻找匹配      │
       └──────────────────┬──────────────────┘
                          ▼
       ┌─────────────────────────────────────┐
       │       第二层：端点内模型选择           │
       │   匹配 canonical_id → 选择端点模型     │
       │   若指定 model 不存在 → 降级到同端点   │
       │   的次一级模型 / 降级到下一优先级端点    │
       └─────────────────────────────────────┘
```

### 3.2 自动升级（未来可选项）

请求未指定 `model` 参数时的默认策略：

| 请求特征 | 升级行为 |
|:--|:--|
| 简单对话、短文本（< 500 tokens） | tier 1 模型 |
| 复杂指令、多轮推理 | 自动升级到 tier 2 |
| 显式设置 `reasoning_effort` 参数 | 匹配 tier 3+ 推理模型 |
| 长上下文（> 4K tokens） | 根据上下文长度匹配对应模型 |

### 3.3 故障降级

```
Kcne 提供: flash(1), pro(2), r1(3)
Kcne 故障:

  选项 A: 降级到同端点下一级模型
    Kcne → flash 请求失败 → Kcne → pro 重试（同端点）

  选项 B: 降级到下一端点同等级模型
    Kcne → flash 失败 → x6m6x → flash（寻找同等级替代）

  选项 C: 兜底
    所有端点 flash 均不可用 → official → flash
```

实际可能组合使用：A → B → C 逐级降级。

---

## 4. 与现有系统的兼容性

### 4.1 向后兼容

- 不修改现有 API 接口格式
- 端点配置中 `model` 字段保持原语义
- 若不声明 `models` 列表（或 `models` 只有一项），行为退化为现有单模型端点模式
- priority 作为端点级故障转移优先级继续保留

### 4.2 迁移路径

1. 先建 models + aliases 表（纯数据，无调度影响）
2. 端点配置可选增加 `models` 字段（不影响现有单模型端点）
3. 调度逻辑增加"多模型端点"路径（单模型端点走旧路径）
4. 逐步扩大覆盖，旧配置无缝保留

---

## 5. 未解决问题（开放讨论）

- **请求复杂度判断**：自动升级需要判断请求是否"复杂"，规则过多可能引入误判。是否有比规则引擎更轻量的方案？
- **模型能力表维护责任**：是否应该开放社区贡献（PR 补模型数据），还是仅本地维护？
- **成本感知**：tier 3 模型成本可能是 tier 1 的 5-10 倍。是否需要引入"费用上限"或"每日配额"限制自动升级？
