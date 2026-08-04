#!/usr/bin/env python3
"""Add auto-refresh interval for endpoint/pool/chain status"""
import sys

path = "/vol1/1000/tool/api-pool/api_pool_server.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

old = "// 移除全局定时刷新 — 只在需要时手动刷新，日志单独轮询\n// 日志保持单独轮询\nsetInterval(() => {\n    loadChatLogs(chatLogsPage);\n}, 3000);"
new = "// 每5秒自动刷新状态（端点、聚合池、切换链），日志单独轮询\nsetInterval(refresh, 5000);\n// 日志保持单独轮询\nsetInterval(() => {\n    loadChatLogs(chatLogsPage);\n}, 3000);"

if old not in content:
    print("FAIL: cannot find old interval block")
    sys.exit(1)
content = content.replace(old, new, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK")