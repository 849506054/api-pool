# 客户端身份与端点自主出站

## 透传与 profile

身份归属在池侧。`client_profile` 空值选透传，非空选 profile；端点自定义头是最终覆盖层。

透传保留安全端到端身份头原值，包括 Accept-Encoding 与 anthropic-version；无 User-Agent 时维持缺省。Host、Content-Length、连接级字段与端点鉴权由网关管理。`_DecodedResponse` 支持 gzip/deflate/br/zstd 和组合编码，SSE 使用 read1 增量读取。

profile 保留样本的安全端到端头，剥离鉴权、Cookie、逐请求追踪与连接级字段。未知、空或仅含保留头的 profile 在发送前报错。`resolve_outbound_headers` 与 `_apply_profile_body_identity` 统一头和桥接后合法请求体身份字段。

## 身份同步

🔄 采最近一次任意真实客户端请求，样本只在内存。`POST /api/client-profiles/sync-identity` 同步后 headers 与 body_identity 随 profile 持久化，列表元信息为 `client_sample`。

同步用样本完整替换，旧头中样本未携带项清除。请求体采集 user、safety_identifier 和 metadata 内明确的用户、客户端、设备、会话身份键；业务对话、工具和其他 metadata 保持在原业务请求。手动编辑头保留已同步 body_identity。

## 关闭检测与池自身出站

`health_mode=none` 只允许真实客户端业务。池自身探活、测试、拉模型及同源积分读取在出站前拦截，重试重新检查；冷却到期直接恢复。

`_probe_disabled` 和 `_guard_pool_outbound` 收口判据。配置对象用自身开关；仅 URL/key 的临时请求按同连接检查，有关闭检测项即跳过。表单 health_mode 透传到拉模型/测试 API。共享积分读取按源站判断，源失败或禁止读取时可保留已有快照。

跳过证据是隔离上游命中数为 0 与 `池自身出站已跳过：... health_mode=none`。即使探活继承客户端上下文，is_probe 仍触发拦截。

## 正式测试入口与覆盖

`test/test_identity_wire.py`、`test/test_profile_quiet_wire.py` 覆盖 HTTP 应用层已识别身份、解压、同步和自主出站静默；`test/test_client_profile_sync.py` / `test/test_profile_sync_ui.js` 覆盖同步接线。前两者支持 `API_POOL_TEST_SOURCE`，隔离方式见 [testing](../testing.md)。

目标客户端的 TLS、账号验证和未知动态签名需独立验证。现场采样、私有 profile 与部署事件由维护技能和本地证据承载。
