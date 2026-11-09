# 出站请求凭据脱敏（现行设计）

作用：池转发给上游的请求里，账号、密码、API Key 等凭据换成固定占位符，模型看到的是占位符而不是真值。

## 边界

- **作用面＝发往上游的请求体**。本地日志（chat_logs、运行日志、请求留档）保持原样：日志只存本地，对外读取本身也要过池。
- **连接上游的鉴权照发**：`Authorization` / `x-api-key` / `x-goog-api-key` 由池按端点 key 写入，属池托管头，不参与脱敏。池不读 Hermes 的 `.env`。
- **单向替换**。用户将凭据保存在 `.env`，只向模型提供变量名与用途；命令和脚本在执行过程中读取原值，模型上下文保留变量引用。
- **全局开关 + 页面入口**，所有端点的发送入口读取同一配置。

## 落点

入口内容过滤改的是请求本体（改后的内容同时进日志），因此脱敏**另留一份出站副本**，落在每次网络发送前：四协议桥接与身份 body 加工（`_apply_profile_body_identity`）完成之后、`_try_endpoint` 的真正 send 之前。

- 关闭时出站字节与原请求一致，日志、原 payload、路由、上游响应全部不动。
- 开启时从原体生成副本再替换，重试与换端点每次都重新取开关值并重新生成——不缓存在请求开头只算一次。
- 开关热更新只在落盘成功后发布内存态，已发出的请求照常完成。

## 配置与接口

- `api_config.json` 顶层 `credential_redaction: {"enabled": false}`；缺键按关闭处理。
- `GET /api/credential-redaction` 与 `PUT` 同形：`{"credential_redaction":{"enabled":bool}}`；结构/类型非法 400，持久化失败 500，成功响应与 GET 同形。
- `save_config()` 的 payload 是**从空 dict 重建**的，新增顶层键必须同时加进这个函数，否则任何一次端点/组保存都会把它静默丢掉（同 `pool_group_defs` / `client_profiles` / `probe_client_profile` 的 `None=不落盘` 写法）。
- 页面头部与「🛡 敏感词过滤」并列的「请求脱敏」入口，弹层内原生 `<select>`（否/是），初始态读接口返回值，保存用返回值确认；说明里写清「本地日志保持原样」「下一次出站生效」「本地工具从 `.env` 取值」「裸值与图片里的凭据识别不到」。

## 识别与改写

- 按**明确字段/标记 + 凭据格式**识别：`password`/`secret`/`token`/`api_key` 一类键名，`sk-…`、`AKIA…`、`ghp_…` 一类形态，`Authorization`/`Basic`/`Bearer`、Cookie/session、带凭据的 URL、JSON/YAML/.env 片段、CLI 参数。普通姓名、IP、路径、UUID、模型名不按裸值猜。
- **中文行内标签按空白取值**：`账号`/`密码`/`密钥`/`令牌`/`口令` 后接空格（含全角空格）再跟一段 ASCII 串时按凭据替换；值限定 ASCII 才动，中文行文（`密码 忘记了`）与本地引用（`密码 $DB_PASSWORD`）原样保留。冒号不是必需分隔符——只对「标签+冒号」建规则会漏掉聊天里最常见的无冒号写法。
- **只改字符串值**：消息正文、system/instructions、文本多模态块、推理文字、工具调用参数、工具结果、描述、metadata 里的敏感值。协议 id、role、model、工具名、schema 的键与 `type`/`required`/`enum`、图片 base64、签名保持原值；工具参数是 JSON 字符串时解析后只改值、保持仍是有效 JSON。
- **本地取值引用原样保留**：`$VAR`、`${VAR}`、`os.environ[...]`、`os.getenv(...)` 与代码里的变量名不动，否则「读 .env 使用」这条路径被自己改坏。保真清单按「本地怎么取凭据」穷举，漏一种就是一次误脱敏：`dotenv_values()` 调用、全大写变量赋值行（`API_KEY=…` 左侧名）、`curl -H "Authorization: Bearer $TOKEN"` 引用、代码里的同名变量。
- **取值调用的默认参数按凭据上下文分别处置**：普通默认值不是凭据，`int(os.getenv("TIMEOUT", "30"))`、`os.getenv("LOG_LEVEL", "INFO")`、`os.getenv("BASE_URL", "https://…")` 一律原样留下——整类当凭据替换会把正常配置改坏，`"30"` 变占位符后 `int()` 直接崩。只有两种情况才替换默认值：外层赋值目标或环境键名本身是凭据字段（`password = os.getenv("SETTING", "…")`、`os.getenv("DB_PASSWORD", "…")`），或字面量自身命中凭据格式（`sk-…`/`ghp_…`/JWT/私钥）。普通键的默认值里仍可能嵌凭据查询（`getenv("A", getenv("DB_PASSWORD", "…"))`），必须递归查进去，不因外层键普通就整段跳过。
- **用 AST 定位字面量，只改命中凭据上下文的字符串节点**：把表达式解析成语法树，取 `os.getenv`/`os.environ.get` 的键与默认值、以及字符串常量节点，再按「外层赋值名/环境键是否凭据字段 + 字面量是否凭据格式」逐个判定。按行正则扫字符串常量的做法要么整段误改、要么漏掉嵌套查询；改写按字节偏移原地替换，保证其余字符逐字不变。
- 占位符按类型稳定生成，重复处理幂等。
- 识别不到的如实告知：未标注的裸密码、图片里的凭据。
- **读回含凭据的文件时看到的是占位符**：工具结果与工具调用参数同在替换面内，`cat` / 读文件回显的凭据是占位符而非原文——核对补丁或验收文件内容时改用哈希 / 本地断言，凭回显判断「文件被改坏」会误报。

### 遍历与结构保真

- **遍历按值分派，不按值假设类型**：`type`、`role` 这类结构键的值可能是任意 JSON（字符串、数组、对象、bool、null），遇到非常规类型只跳过该键、继续遍历，不把值当字符串查表。直接拿值做 dict/set 查找会抛 unhashable 而整条请求失败——fail-closed 下等于正常请求被 503。
- **业务 `type` 不是结构白名单**：`type: file`、`document` 一类业务对象仍要进值扫描。结构白名单只认协议结构键（id/role/model/工具名/schema 的 `type`·`required`·`enum`），按「这个键是不是业务载荷」判断，不按 `type` 的取值判断。
- **文本键要收全别名**：同一段文字在四协议里的键名不同（`text`/`content`/`system`/`instructions`/`thinking`/`reasoning`/`reasoning_content`/`reasoning_text`/`refusal`/`description`/`output`/`summary`）。新增协议别名时先加进文本键集合再跑用例，漏一个就是整类消息不脱敏。
- **格式覆盖面**：`.env`/shell 赋值行（含引号值）、YAML 块标量（`key: |` 后多行）、带凭据的 URL、JSON 片段、CLI 参数都算凭据载体；只剩一处漏识别，用户就会问「为什么这行没换」。

## 失败处置

本地脱敏异常返回 503，**原始请求零出站**；这是池侧处理失败，不记端点失败、不冷却、不触发换端点绕行。实现时注意外围的 `except` 会把异常当上游错误分类并进入重试/轮转，需要单独的类型与分支把它接住。

**在途计数窗口**：轮转循环里 `_acquire_inflight` 在 send 之前登记，图像解析预处理（`_translate_images_sync`，`is_vision=False` 端点）就在登记与 `try` 之间，此处抛出的异常必须显式 `self._release_inflight(ep.id, group)` 后再上抛，否则该端点被伪占用、后续组的请求被当成「已被别的组占用」永久避让。上下文超限的跳过分支就是同一位置的现成写法。视觉解析调用的 `_try_endpoint` 里捕获脱敏异常时原样重抛（`except CredentialRedactionError: raise`），避免被它的 provider 失败处理吞成「图片解析失败」。改用例时对这条路径做一次真实请求断言：503 后 `_inflight_owner` 为空、端点 `_fail_count` 未增。

**已在响应流中的失败要把错误交给客户端**：非流式直接 503；流式已经吐过帧之后（含 Responses 惰性路径）不能静默断流，必须补一个协议合法的失败帧/错误事件并带上 `code`，让客户端看到同一错误码，而不是收到「响应为空」再触发原端点重放。验证时断言「已发帧数不变（`new_sends` 不增）+ 客户端收到 503 与错误码」两件事。

## 正式验证契约

- 黄金样本先行：新增用例在原版上失败，再验证目标行为。
- 四协议（chat / anthropic / 原生 responses / gemini）用 mock 上游**捕获真实出站 HTTP body**，流式与非流式都覆盖；重试与异构 fallback 每次出站都要检查；同时断言原始 payload 与日志保真。
- 结构保真用例：工具参数 JSON 仍可解析、schema 与 id 未变、`$VAR`/`os.environ` 原样。
- **改写后的代码仍能跑**：每个用例对脱敏结果做 `compile()`，断言 `redact(clean) == clean`（幂等），并把 `int(os.getenv("TIMEOUT", "30"))` 这类普通取值放进桩 `os` 里 `exec` 一次、断言取到 `30`；只断言「原串不变」抓不到占位符污染默认值。ruff 的 S102 会拦测试里的 `exec`，该行加 `# noqa: S102`。
- **边界回归集**（识别器最容易漏/误改的一批，改规则后整组复跑）：非常规类型结构键（`type` 为数组/对象/bool/null）、业务 `type: file` 载荷、`.env` 混合字面量与引号、`getenv` 默认参数与 `dotenv_values`、全大写赋值与 curl 引用、YAML 块标量、`reasoning_text` 别名、视觉预处理失败后的在途归还、流内失败帧带错误码。
- **新增边界用例先红后绿**：先单跑确认在现规则下 FAIL，再补键名/分支让它 PASS；直接写「能通过」的用例等于没覆盖（与「反向验证」同一要求）。
- 脱敏异常用例断言零网络发送且端点无失败记账。
- 隔离测试把 `API_POOL_DB_DIR` 指到 scratch，并关掉后台线程，避免动生产池与外部写。用例落 `test/test_credential_redaction.py`（后端）与 `test/test_credential_redaction_ui.js`（页面开关），页面自检照「前端 smoke 套件」的静态覆盖法抽 `<script>`。
- 扫描器 `credential_redaction.py` 可由后端、边界测试与合成上游夹具独立导入，规则测试只加载扫描器。

测试命令与隔离、部署及用户验收分层见 [testing](../testing.md)。
