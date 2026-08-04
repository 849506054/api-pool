# 假成功响应检测（Fake-Success Detection）

## 问题描述

上游返回 HTTP 200 + 合法 JSON，但 content 字段中包含的是错误/拒绝文本，而非正常 LLM 输出。这种"假成功"不会被 `_try_endpoint` 的现有错误检测捕获（它只检查 HTTP 状态码、JSON 解析、超时/连接错误），直接透传给客户端，表现为模型返回了无意义的结果。

示例：
```json
HTTP 200
{
  "choices": [{"message": {"content": "你好，我无法给到相关内容"}}]
}
```

## 设计原则：每端点独立配置

默认关闭，需手动在端点编辑 UI 中开启：

- **`Endpoint.check_fake_success: bool = False`** — 数据类字段，默认 `False`
- 端点编辑表单新增「假成功检测」下拉开关（关闭/开启），保存时写入 `check_fake_success` 字段
- `_try_endpoint` 中的检测逻辑受 `ep.check_fake_success` 守卫，仅开启时才执行
- 已有端点不受影响（默认值 `False`）

## 检测位置

在 `_try_endpoint` 中，非流式场景，`return body, ""` 返回之前：

- **OpenAI 格式**：约 line 1270，`content = body["choices"][0]["message"].get("content", "")` 之后
- **Anthropic 格式**：约 line 1258，`reply.strip()` 之后，`return o_body, ""` 之前

两处都加了 `if ep.check_fake_success:` 守卫。

## 检测逻辑

```python
# 假成功检测（仅端点启用时）
if ep.check_fake_success:
    _content_text = (content or reasoning or "").strip()
    if _content_text and any(p in _content_text for p in FAKE_SUCCESS_PATTERNS):
        sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
        return None, "fake-success: 内容匹配拒绝模式"
```

## 常量定义

```python
# 假成功检测：上游返回 200 OK 但内容含拒绝/错误信息
FAKE_SUCCESS_PATTERNS = ["无法给到相关内容"]
```

位于 `api_pool_server.py` 模块级（约 line 25），可追加新模式。

## 处理方式

返回 `(None, "fake-success: ...")` 而非 `(body, "")`，调用方 `chat()` 会：
1. 将错误加入 `errors` 列表
2. 调用 `_rotate(ep, error)` 触发冷却
3. 轮转至下一个可用端点

## 性能

- 每次请求多花 **微秒级**（Python `in` 字符串查找）
- 远低于 HTTP 请求（百毫秒级）、JSON 解析（毫秒级）、日志写入（毫秒级）
- 未开启的端点零开销（仅一次 `if` 判断）

## 已有先例

`check_vision_support`（line 1348）已使用关键词匹配检测模型"假装成功但实际不能读图"：
```python
unsupported_keywords = ["cannot see", "can't see", ..., "无法查看", "无法读取", "抱歉", "sorry", ...]
```

## 注意事项

### 流式场景
流式响应在 chunk 中逐帧返回 content，无法在返回前整体检查。当前不处理流式。

### 假阳性风险
- 模式列表应仅包含明确的上游系统错误文本，而非模型正常拒绝用语
- 每端点启用机制降低了全局假阳性风险

### 模式维护
- 发现新模式后追加到 `FAKE_SUCCESS_PATTERNS` 列表
- 优先使用完整短语，避免子串误匹配