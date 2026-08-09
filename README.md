# API Pool

一个轻量、零依赖的 DeepSeek 端点集中管理工具与 API 网关。

> **定位**：本工具专注于 DeepSeek（及兼容）模型端点的集中管理、健康检测、优先级调度，
> 不再作为通用多模型聚合入口。多模型混合场景建议走 Hermes 原生 fallback 链。

![Python](https://img.shields.io/badge/Python-3.13-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Zero Deps](https://img.shields.io/badge/Dependencies-None-brightgreen)
![SQLite](https://img.shields.io/badge/Database-SQLite-blue)

---

## 核心功能

- **集中管理** — 统一维护多个 DeepSeek API 端点，UI 可视化管理
- **自动健康检测** — 内置周期性连通性检测，支持零成本 Models 探针
- **优先级调度 + 故障恢复回迁** — 按 priority 自动轮选，故障熔断冷却，恢复后自动回迁
- **延迟切换（Deferred Failback）** — 端点冷却到期探活通过后，不立即切回（避免破坏当前端点 prompt cache），仅在会话空闲 5 分钟后才自动切回；支持端点级开关（`deferrable`），适用于昂贵兜底端点
- **上下文长度限制（端点可选）** — 配置 `max_context_k` 后，超过长度限制的请求自动跳过该端点，避免账号被限制
- **多协议兼容** — 支持 OpenAI 兼容协议与 Anthropic 协议（管理存量端点），对外统一 OpenAI 接口
- **自动图片预处理** — 目标端点不支持视觉时自动调用视觉模型解析
- **ToolCall ID 前缀重写（端点可选）** — 端点配置 `tool_call_id_prefix` 时，请求中 tool_call id 确定性重写为该前缀（如 DeepSeek 官方 `call_00_ET_`），解决跨端点切换后历史混入其他端点格式 id 导致的 400（Kcne 场景）
- **统计大盘** — Token 消耗、缓存命中、请求数趋势
- **零依赖** — 只需 Python 3.13，单文件即可运行

## 快速开始

```bash
git clone https://github.com/849506054/api-pool.git
cd api-pool
python api_pool_server.py
```

访问 http://localhost:5100 打开管理面板。
API 接口：http://localhost:5100/v1/chat/completions

## 部署

生产环境推荐通过 systemd 管理：

```bash
cp api-pool.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now api-pool.service
```

## 端点配置字段

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `deferrable` | bool | `true` | 冷却到期后是否延迟切换（保 cache）。false=上游恢复立即切回（适用于昂贵兜底端点） |
| `max_context_k` | int | `0` | 最大上下文长度（K=1000 tokens），0=不限。超过时自动跳过该端点 |

## 堆栈

| 组件 | 选择 |
|------|------|
| 后端 | Python 标准库 (urllib, http.server, threading) |
| 前端 | 单文件内嵌 HTML/JS/CSS |
| 存储 | SQLite（token_stats.db, chat_logs.db） |
| 部署 | systemd 单进程，Restart=always |

## 项目状态

当前阶段：功能维护。详见 PROJECT.md