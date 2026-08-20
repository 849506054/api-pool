# API Pool

一个轻量、零依赖的多模型 API 聚合网关与 Endpoint 路由管理工具。

> **定位**：API Pool 2.0 对外提供稳定的 OpenAI-compatible Chat Completions 入口，
> 对内按 Endpoint 配置进行模型路由、协议转换、健康管理、故障转移和统计。Endpoint
> 可以使用不同的模型和协议，不要求整个池只服务于某一种模型。

![Python](https://img.shields.io/badge/Python-3.13-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Zero Deps](https://img.shields.io/badge/Dependencies-None-brightgreen)
![SQLite](https://img.shields.io/badge/Database-SQLite-blue)

---

## 核心功能

- **Endpoint 集中管理** — UI 可视化维护多个上游 Endpoint，各自配置 URL、Key、模型、协议、代理和兼容参数
- **自动健康检测** — 内置周期性连通性检测和 Endpoint 状态管理，避免把请求持续发送到不可用上游
- **优先级调度与故障转移** — 按 priority 选择 Endpoint，支持失败重试、冷却、恢复探活和自动回迁
- **延迟切换（Deferred Failback）** — 上游恢复后可延迟切回，减少切换造成的 prompt cache 损失；支持 Endpoint 级开关 `deferrable`
- **上下文长度限制（Endpoint 可选）** — 配置 `max_context_k` 后，超出 Endpoint 上限的请求会跳过该 Endpoint
- **多协议转换** — 对外统一 OpenAI-compatible 接口，可管理 OpenAI-compatible 与 Anthropic 上游 Endpoint；已验收文本、流式、工具调用、多轮工具结果、跨协议工具历史、base64 PNG 图片和 Hermes 实际工具循环，详见 [`docs/anthropic-compatibility-matrix.md`](docs/anthropic-compatibility-matrix.md)
- **多模态兼容处理** — 目标 Endpoint 不支持视觉时，可配置视觉模型进行图片预处理
- **工具调用兼容** — 支持 tool call 转换、工具结果回传，以及按 Endpoint 配置 `tool_call_id_prefix` 修正跨上游切换后的 ID 格式
- **独立入口与内部路由** — Hermes 只需连接稳定的 `api-pool` 入口模型名，API Pool 2.0 使用目标 Endpoint 自己的 `model` 转发
- **统计大盘** — Token 消耗、缓存命中、请求数趋势
- **零依赖** — 只需 Python 3.13，单文件即可运行

## 快速开始

```bash
git clone https://github.com/849506054/api-pool.git
cd api-pool
python api_pool_server.py
```

访问 http://localhost:5200 打开 API Pool 2.0 管理面板。
API 接口：http://localhost:5200/v1/chat/completions

## 部署

API Pool 2.0 生产环境推荐通过独立的 systemd 单元管理：

```bash
cp api-pool2.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now api-pool2.service
```

## Hermes 侧配置

API Pool 2.0 对 Hermes 暴露的是 **OpenAI-compatible Chat Completions** 接口。Hermes 只需要配置一个稳定的入口模型名，例如 `api-pool`；真正发送给哪个上游模型、是否切换 Endpoint，由 API Pool 2.0 根据 Endpoint 配置和故障转移策略决定。

### 1. 最小可用配置

在 Hermes 的 `config.yaml` 中添加自定义 Provider。API Key 建议放在 Hermes 的 `.env`，不要直接写入 YAML：

```yaml
model:
  default: api-pool
  provider: custom:api-pool2
  base_url: http://192.168.5.6:5200/v1
  context_length: 1048576

custom_providers:
  - name: api-pool2
    base_url: http://192.168.5.6:5200/v1
    api_key: ${API_POOL2_API_KEY}
    model: api-pool
```

对应的 Hermes `.env`：

```dotenv
API_POOL2_API_KEY=replace-with-your-api-pool-gateway-key
```

如果 API Pool 2 没有启用网关鉴权，也建议保留 `api_key` 字段并使用一个非空占位值；如果当前实例要求鉴权，则这里填写的是 API Pool 2 网关接受的 Key，不是某个上游 Endpoint 的 Key。上游 Endpoint 的真实 Key 只在 API Pool 2 管理面板中配置。

字段说明：

| 字段 | 用途 |
|------|------|
| `model.default` | Hermes 对外使用的稳定符号模型名。建议固定为 `api-pool`，不要随 API Pool 当前选中的 Endpoint 改名。 |
| `model.provider` | Hermes 运行时 Provider 名称，必须与 `custom_providers[].name` 对应，即 `custom:api-pool2`。 |
| `model.base_url` | 必须与自定义 Provider 的 URL 一致，并指向 API Pool 2 的 `/v1`；不要只写主机根地址。 |
| `model.context_length` | 符号模型无法从 `/v1/models` 自动解析时的显式上下文上限。API Pool 2 当前不提供可用于该解析的 `/v1/models` 路由，建议显式设置。 |
| `custom_providers[].base_url` | API Pool 2 的 OpenAI-compatible 根路径，固定为 `http://<host>:5200/v1` 或反向代理后的对应 `/v1`。 |
| `custom_providers[].model` | 发给 API Pool 2 的入口模型字段，使用 `api-pool` 即可；API Pool 2 转发时使用目标 Endpoint 自己的 `model`。 |

`context_length` 使用的是 token 数，不是 `max_context_k` 的千 token 单位。示例中的 `1048576` 只是适用于相应上游模型时的示例值；实际部署应按池内所有可能命中的模型的最小上下文上限填写。

### 2. 将 API Pool 2 放入 Hermes fallback

如果 API Pool 2 只是某条 fallback 链中的一跳，显式写出 Provider 和模型，不要只写一个模糊的 provider 名：

```yaml
fallback_providers:
  - provider: custom:api-pool2
    model: api-pool
    base_url: http://192.168.5.6:5200/v1
    api_key: ${API_POOL2_API_KEY}
  - provider: deepseek
    model: deepseek-v4-flash
```

Hermes 会按列表顺序尝试。保留现有 fallback 顺序时，只把 API Pool 2 插入明确需要的位置；不要把所有不同模型都伪装成同一个 `api-pool` 名称，否则 Hermes 无法针对每一跳应用正确的模型族参数、上下文元数据和 reasoning 行为。

如果 API Pool 2 已经作为主模型使用，API Pool 内部 Endpoint 故障转移和 Hermes 外部 `fallback_providers` 是两层不同机制：

- API Pool 2 内部：在同一个网关内按 Endpoint 优先级、冷却和协议适配切换；
- Hermes fallback：API Pool 2 整体不可用或请求失败后，切换到下一组独立的 Provider/模型。

### 3. DeepSeek thinking 参数

不要在 Hermes 的 `custom_providers` 上无条件给所有混合 Endpoint 注入 DeepSeek 专属 `thinking` 或 `reasoning_effort`。API Pool 2 可能同时管理 DeepSeek、OpenAI-compatible GPT 或视觉 Endpoint，全局注入会导致非 DeepSeek Endpoint 返回 400。

只有当该 Hermes Provider 确定只连接 DeepSeek 兼容模型时，才考虑按 Provider 配置供应商参数：

```yaml
custom_providers:
  - name: api-pool2-deepseek
    base_url: http://192.168.5.6:5200/v1
    api_key: ${API_POOL2_API_KEY}
    model: api-pool
    extra_body:
      thinking:
        type: enabled
```

混合池推荐保持上述参数为空，让 API Pool 2 按目标 Endpoint 的协议和模型处理；不要把 `thinking` 写进 API Pool 服务端的全局请求构造逻辑。

### 4. 配置后验证

修改 Hermes 配置后，先执行配置检查，再验证 Provider 和 fallback 视图：

```bash
hermes config check
hermes fallback list
```

从 Hermes 所在机器直接验证 API Pool 2 的入口：

```bash
BASE_URL=http://192.168.5.6:5200/v1
KEY="$API_POOL2_API_KEY"

curl -sS "$BASE_URL/chat/completions" \\
  -H "Authorization: Bearer $KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"api-pool","messages":[{"role":"user","content":"只回复 OK"}],"max_tokens":8,"temperature":0}'
```

验证重点：

- URL 使用 `/v1/chat/completions`，不要请求 `/chat/completions` 或把 `/v1` 重复拼接；
- Hermes 配置中的 `provider` 为 `custom:api-pool2`，不是 `custom` 或 `api-pool2`；
- `hermes fallback list` 显示的主 Provider、模型和顺序与 YAML 一致；
- Hermes 日志不再出现 `Could not determine context length ... falling back to 256,000`；
- API Pool 2 管理面板的请求日志显示实际命中的目标 Endpoint，而不是只看到符号名 `api-pool`。

修改 `config.yaml` 后需要重启 Hermes Gateway 才会对 Gateway、Telegram 和 cron 请求生效；当前会话可用 `/model` 重新选择模型，但这不替代 Gateway 的配置重载。

### 5. 常见错误

| 现象 | 原因与处理 |
|------|------------|
| `404 Not found` | `base_url` 少了 `/v1`、重复了 `/v1`，或把 1.0 的 `5100` 端口写进了 2.0 配置。 |
| `Could not determine context length` | `model` 使用了符号名 `api-pool`，且没有设置 `model.context_length`；补上显式值。 |
| 已设置 `context_length` 但仍 fallback 到 256K | `model` 段缺少与 Provider 一致的 `base_url`，运行时路由匹配可能清除 context pin；补齐 `model.base_url`。 |
| `401` 或 `403` | Hermes 的 `api_key` 与 API Pool 2 网关鉴权不匹配；这不是上游 Endpoint Key 的问题。 |
| 上游 Endpoint 返回 400 | 检查 API Pool 2 UI 中该 Endpoint 的 `protocol`、`model`、`extra_payload` 和 thinking 设置；不要先在 Hermes 全局注入参数。 |
| Hermes fallback 没按预期切换 | 检查 `hermes fallback list`；每一跳都要有明确的 `provider` 和 `model`，并确认目标 Endpoint 没有处于冷却状态。 |
### API Pool Endpoint 配置字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | string | - | Endpoint 显示名称。 |
| `base_url` | string | - | 上游 API 根地址；按 Endpoint 协议填写对应的 API 入口。 |
| `api_key` | string | - | 该 Endpoint 的上游密钥；不要与 Hermes 连接 API Pool 的网关密钥混淆。 |
| `model` | string | - | 发送给该上游的实际模型名；不会被 Hermes 的符号入口模型名覆盖。 |
| `protocol` | string | `openai` | 上游协议类型，支持 OpenAI-compatible 和 Anthropic 兼容转换路径。 |
| `priority` | int | - | Endpoint 选择优先级，具体数值关系以管理面板和当前路由实现为准。 |
| `deferrable` | bool | `true` | 冷却到期后是否延迟切换（保 cache）。false=上游恢复立即切回。 |
| `max_context_k` | int | `0` | 最大上下文长度（K=1000 tokens），0=不限。超过时自动跳过该 Endpoint。 |
| `tool_call_id_prefix` | string | 空 | 按 Endpoint 重写 tool call ID 前缀，用于兼容特定上游格式。 |
| `extra_payload` | object | `{}` | 注入该 Endpoint 的供应商参数；应按目标模型/协议配置，不要把 DeepSeek 专属字段全局化。 |

## 堆栈

| 组件 | 选择 |
|------|------|
| 后端 | Python 标准库 (urllib, http.server, threading) |
| 前端 | 单文件内嵌 HTML/JS/CSS |
| 存储 | SQLite（token_stats.db, chat_logs.db） |
| 部署 | systemd 单进程，Restart=always |

## 项目状态

当前阶段：功能维护。详见 PROJECT.md