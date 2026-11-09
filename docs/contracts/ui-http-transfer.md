# 页面压缩与缓存校验

## 响应契约

`_ui_html_representation` 生成 HTML 原文、gzip 正文与基于原文 SHA-256 的弱 ETag，LRU 缓存复用编码结果。`_load_gui_html` 按页面 mtime 热加载，新页面对应新 ETag。

浏览器按 Accept-Encoding 协商 gzip，原文至少 1KB 且压缩有效时使用 gzip。明确 `gzip;q=0` 优先于通配符。HTML 使用 `private, no-cache` 和 `Vary: Accept-Encoding`；If-None-Match 命中时 304 且正文为空，页面变化时返回最新 200。

管理 GET `/api/*` 的普通 JSON 在 `_send_json` 内压缩并使用 `no-store`，每次读取实时响应。模型 API、管理写请求响应与 SSE 保持原传输契约，状态刷新频率沿原页面逻辑。

入口鉴权先于页面缓存校验，有效 ETag 表示内容版本，访问权限由入口验证。

## 正式测试入口

`test/test_ui_http_transfer.py` 使用真实 Handler 与 loopback HTTP，覆盖压缩/原文一致、条件 GET 零正文、页面变化与磁盘热更新、协商质量值、管理 JSON 实时性、模型/写请求原文和 SSE 原始帧。它用 AST 加载 HTTP 层以隔离模块启动副作用。

命令见 [testing](../testing.md)。本地 `workspace/tools/verify_ui_http_transfer.py` 对服务只发 GET，并会创建临时身份文件及本地验证记录；凭据与执行配方归维护技能；2026-11-08 的字节读数与用户验收见 [历史入口](../history/README.md)。
