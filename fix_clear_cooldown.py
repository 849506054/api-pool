#!/usr/bin/env python3
"""Fix clearCooldown: only reset target endpoint, not all endpoints"""
import sys

path = "/vol1/1000/tool/api-pool/api_pool_server.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Backend: when cooldown_minutes is set to 0 via update_endpoint, also clear _cooldown_until and _fail_count
old_backend = "                    # 先处理所有字段\n                    for k, v in updates.items():\n                        if hasattr(ep, k) and not k.startswith(\"_\") and k != \"id\":\n                            setattr(ep, k, v)"
new_backend = "                    # 先处理所有字段\n                    for k, v in updates.items():\n                        if hasattr(ep, k) and not k.startswith(\"_\") and k != \"id\":\n                            setattr(ep, k, v)\n                    # cooldown_minutes 被设为 0 时视为解冻，同步清除运行态\n                    if updates.get(\"cooldown_minutes\") == 0:\n                        ep._cooldown_until = 0\n                        ep._fail_count = 0\n                        ep._last_error = \"\"\n                        ep._last_error_ts = 0"
if old_backend not in content:
    print("FAIL: cannot find backend update loop")
    sys.exit(1)
content = content.replace(old_backend, new_backend, 1)

# 2. Frontend: remove global reset call, just set cooldown_minutes=0 and refresh
old_js = "async function clearCooldown(id){await api('PUT',`/api/endpoints/${encodeURIComponent(id)}`,{cooldown_minutes:0});await api('POST','/api/reset');setTimeout(async()=>{await api('PUT',`/api/endpoints/${encodeURIComponent(id)}`,{cooldown_minutes:5});refresh();},200);toast('已解除冷却','success');refresh();}"
new_js = "async function clearCooldown(id){await api('PUT',`/api/endpoints/${encodeURIComponent(id)}`,{cooldown_minutes:0});refresh();toast('已解除冷却','success');}"

if old_js not in content:
    print("FAIL: cannot find clearCooldown JS")
    sys.exit(1)
content = content.replace(old_js, new_js, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK")