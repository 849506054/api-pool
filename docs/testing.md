# 测试选择与验收层次

本指南列正式文件与通过信号，按改动路径选必要测试。2026-11-09 文档整理只核验文件、runner、符号和引用，业务套件执行结果由实际交付记录提供。

## 按改动选择

| 改动 | 必要入口（项目相对路径） | 重点信号 |
|---|---|---|
| 单点 UI / 表单 | 对应 `test/*.js`；如 `test_group_rotate_members.js`、`test_credential_redaction_ui.js` | DOM/初始态/保存报文断言通过，语法正确；视觉和交互由用户确认 |
| 页面 HTTP 响应层 | `test/test_ui_http_transfer.py` | 原文/压缩逐字节一致、304 零正文、mtime 更新、实时 JSON 与 SSE 保真 |
| 子组加入 / 名次路由 | `test/test_group_join_main.py`、`test/test_group_join_parent.py`、`test/test_parent_slot_failover.py`、`test/test_group_membership_ui.js` | 加入/移出/排序/失败后名次、迟到回调与落盘按本次路径覆盖 |
| 主动轮换 / 空闲借用 | `test/test_group_rotation.py` / `test/test_group_idle_borrow.py` 与对应 JS | 成功次数、资格、窗口、指针、epoch 和占用归还 |
| 错误 / 重试 / 恢复 | `test/test_client_error_classification.py`、`test/test_retry_on_rate_limit.py`、`test/test_budget_cut_retry.py`、`test/test_retry_recovery_scope.py` | 分类、发送次数、冷却、组归属、最后真实错误 |
| 新字段 / 持久化 | `test/test_endpoint_config_persistence.py`、`test/test_endpoint_thinking_persist.py`、`test/test_atomic_config_write.py` | PUT → 磁盘 → 重新加载/重启的值一致 |
| 身份 / 自主出站 | `test/test_identity_wire.py`、`test/test_profile_quiet_wire.py` | loopback 上游实收身份与 body、禁止出站命中 0、组合解压 |
| 图片 | `test/test_vision_history_wire.py`、`test/test_vision_stream_wrapper_bytes.py`、`test/test_vision_pool_group.py` | 原消息保真、缓存、冷解析、各目标副本与流字节 |
| 凭据扫描规则 / 开关 | `test/test_credential_redaction*.py`、`test/test_credential_redaction_ui.js` | 四协议真实 HTTP body、结构/本地引用/幂等、失败 503 零新发送与占用归还 |
| 协议桥 | `test/test_anthropic_streaming.py`、`test/test_gemini_bridge.py`；Responses 和跨协议使用 pytest 入口 | 文本/工具真调用/续轮/图片/usage/SSE 与错误事件 |

只命中单点 UI 或组语义时选直接相关集；共享路由、冷却、错误分类或持久化改动才评估全量。已批准的必要测试范围按边界执行，需要扩大先获授权。

## 隔离准备

导入 server 会创建数据库并可能启动后台线程；配置和 runtime_state 使用相对文件名，`API_POOL_DB_DIR` 必须先指已存在的临时目录。旧 runner 有在导入时执行的断言、普通函数测试和 unittest 三种形态，使用匹配的入口。

以下是未来代码改动的测试配方；在项目根建立独立运行副本，只复制代码和测试文本，配置、日志与数据库留在 scratch：

```bash
box="$(mktemp -d "${TMPDIR:?}/apipool-tests.XXXXXX")"
mkdir -p "$box/test" "$box/db"
cp api_pool_server.py credential_redaction.py "$box/"
cp -R static "$box/"
cp test/*.py test/*.js "$box/test/"
cd "$box"
export API_POOL_DB_DIR="$box/db"
export PYTHONDONTWRITEBYTECODE=1
```

使用匹配项目依赖的解释器；HTTP 解压测试依赖 brotli / zstandard，协议函数测试依赖 pytest。代码副本/新增模块须同批到位，临时配置使用合成 key 与 loopback；本机安装、真实账号和清理配方走维护技能。

## 命令与通过信号

下列文件在整理时实读存在；命令在上述副本根运行：

```bash
python3 -m unittest discover -s test -p 'test_ui_http_transfer.py' -v
python3 -m unittest discover -s test -p 'test_parent_slot_failover.py' -v
python3 -m unittest discover -s test -p 'test_group_rotation.py' -v
python3 -m unittest discover -s test -p 'test_group_idle_borrow.py' -v
python3 -m unittest discover -s test -p 'test_retry_on_rate_limit.py' -v
python3 -m unittest discover -s test -p 'test_endpoint_config_persistence.py' -v
python3 -m unittest discover -s test -p 'test_identity_wire.py' -v
python3 -m unittest discover -s test -p 'test_profile_quiet_wire.py' -v
python3 -m unittest discover -s test -p 'test_vision_history_wire.py' -v
python3 -m unittest discover -s test -p 'test_credential_redaction*.py' -v
node test/test_group_rotate_members.js
node test/test_credential_redaction_ui.js
node test/test_group_membership_ui.js
python3 test/test_gemini_bridge.py
python3 -m pytest -q test/test_responses_api.py test/test_cross_protocol_fallback.py
```

unittest 以进程退出 0、实际选中用例数和 OK 为通过；Gemini runner 显示 `0 failed` 并退出 0；pytest 显示选中项 passed、零失败/错误并退出 0；Node 断言无异常且退出 0。源码存在与语法正确只证明接线层，实线断言还要核真实发送次数、body 和状态。其他 unittest 文件沿同一 `discover -s test -p '<确切文件名>'` 形式选择。

全量获批时分别运行 unittest discover、pytest 协议函数集、Gemini 自带 runner 和按文件遍历的 Node 套件。`node test/*.js` 只将首文件当主程序，批量应逐文件执行：

```bash
python3 -m unittest discover -s test -p 'test_*.py' -v
for file in test/*.js; do node "$file" || exit "$?"; done
```

报告列真实选中数、失败/错误明细与同环境原版对照。历史 Hermes stub 或数据库夹具缺口以当前实跑为准，基线错误保持独立归类。

## 黄金样本、实线与部署

1. 黄金样本：冻结未改动原版，新增反例在原版上失败，目标代码通过。按函数/接口断言，不以随机上游碰撞代替反例。
2. 隔离单测：只加载必要层，合成配置、独立目录与占用回收。凭据 fixture 的 `load_pool` 限制私有读取并屏蔽启动线程；HTTP 传输 fixture 用 AST 只载 Handler 层。
3. 隔离实线：loopback 上游捕获真实 HTTP body/头/SSE，断言业务结果、原 payload/日志、发送次数、冷却与指针。跨协议和真实工具续轮逐场景覆盖。
4. 部署文件验证：运行文件到位/hash/启动证明部署；支持 `API_POOL_TEST_SOURCE` 的身份、图片、重试、HTTP/脱敏测试可指定同字节部署快照，其余 runner 使用副本代码。部署操作、解释器与授权归维护技能。
5. 自然业务与用户验收：真实日志/落库/后续目标证明实际效果；界面视觉、移动端交互和体感由用户确认。无自然触发时明确保持观察缺口，入口见 [PROJECT](../PROJECT.md)。

部署后页面只读 wire 工具为本地 `workspace/tools/verify_ui_http_transfer.py`，覆盖条件请求、JSON 与入口权限。执行读取现有身份、创建临时身份文件并写本地验证记录，同时产生管理请求，按维护技能授权执行；它是本机操作工具，仓库不提供私有身份值。
