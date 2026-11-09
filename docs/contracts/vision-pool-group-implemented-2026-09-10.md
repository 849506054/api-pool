# 视觉池、历史图片与解析缓存

## 内置视觉组

vision 与 main 同级，是唯一内置组，常量为 `VISION_GROUP` / `VISION_SELECTOR`。名称、类型、选择器和删除保持内置约束；上下文长度可显式配置。图片解析候选来自 `_vision_pool_candidates`，视觉池成员按组内优先级选择。

解析失败用既有探活阶梯冷却入口，原因 `vision_translate_failed`；成功清失败态。池无候选或解析失败时保留原图转发，WARN 说明该部分未解析。测试图片由 `_tiny_png_base64` 生成合法样本。

## 历史、缓存与原消息

非视觉目标按单图 URL/data URL 与有效 detail 查询成功描述缓存，键由 `_vision_cache_key` 生成；TTL 与容量由 `_VISION_CACHE_TTL` / `_VISION_CACHE_MAX` 定义。先查缓存，再查候选；候选不可用时仍可复用已有描述。过期、淘汰或重启后重新解析。

未命中的新图与历史图在原消息原位置转成描述，文本、角色、工具结构和助手历史保持。失败、空白或只有思考内容时保留原图；成功部分独立复用。同图冷解析由有界锁串行化，等待受请求截止时间约束。

视觉目标收到完整源消息。配置式定向端点保持独占，只复用已有缓存，未知原图交给指定端点。轮转各端点从同一份原始历史生成各自出站副本。

## 统一业务链

`_translate_images_sync` 处理后接回共同上下文检查、重试、轮转、记账和在途归还。冷解析完成提示只随成功目标流发送，热缓存直接交目标流；验证提前关闭时关闭目标流并归还在途占用。

DEBUG 关闭时成功缓存命中和兼容转换提示保持静默，INFO 保留实际新解析与请求目标/结束，失败由 WARN 与既有错误链说明。

## 正式测试入口

`test/test_vision_pool_group.py`、`test/test_vision_history_wire.py`、`test/test_vision_stream_wrapper_bytes.py`。实线覆盖 cold/cache/no-candidates × Chat/Responses × JSON/SSE、故障轮转、严格 400、超限跳过、全部失败、并发与部分成功；loopback 上游捕获真实字节。`test_vision_history_wire.py` 支持 `API_POOL_TEST_SOURCE`，见 [testing](../testing.md)。
