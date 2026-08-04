#!/usr/bin/env python3
"""Add per-endpoint check_fake_success toggle (default off) to api_pool_server.py"""
import sys

path = "/vol1/1000/tool/api-pool/api_pool_server.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Module-level constant -> remove it (replaced by per-endpoint field)
old_const = "# 假成功检测：上游返回 200 OK 但内容含拒绝/错误信息\nFAKE_SUCCESS_PATTERNS = [\"无法给到相关内容\"]\n"
new_const = "# 假成功检测：上游返回 200 OK 但内容含拒绝/错误信息\nFAKE_SUCCESS_PATTERNS = [\"无法给到相关内容\"]\n"
# Keep it (used by both protocols), but we also add per-endpoint field

# 2. Add check_fake_success field to Endpoint dataclass
old_field = "    in_pool: bool = False  # 是否加入聚合池（默认不加入）\n"
new_field = "    in_pool: bool = False  # 是否加入聚合池（默认不加入）\n    check_fake_success: bool = False  # 是否检测假成功（200 OK 但内容含拒绝信息）\n"
if old_field not in content:
    print("FAIL: cannot find Endpoint.in_pool field")
    sys.exit(1)
content = content.replace(old_field, new_field, 1)

# 3. _ep_to_dict: add check_fake_success output
old_ep_dict = '            "in_pool": ep.in_pool,'
new_ep_dict = '            "in_pool": ep.in_pool,\n            "check_fake_success": ep.check_fake_success,'
if old_ep_dict not in content:
    print("FAIL: cannot find _ep_to_dict in_pool")
    sys.exit(1)
content = content.replace(old_ep_dict, new_ep_dict, 1)

# 4. _sync_to_config: add check_fake_success
old_sync = '\"in_pool\": ep.get(\"in_pool\", False)} for ep in pool.list_endpoints()])'
new_sync = '\"in_pool\": ep.get(\"in_pool\", False), \"check_fake_success\": ep.get(\"check_fake_success\", False)} for ep in pool.list_endpoints()])'
if old_sync not in content:
    print("FAIL: cannot find _sync_to_config in_pool")
    sys.exit(1)
content = content.replace(old_sync, new_sync, 1)

# 5. Anthropic path: add check_fake_success guard to fake-success detection
old_anthropic_check = '''                        # 假成功检测
                        _reply_text = reply.strip()
                        if _reply_text and any(p in _reply_text for p in FAKE_SUCCESS_PATTERNS):
                            sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
                            return None, "fake-success: 内容匹配拒绝模式"
                        return o_body, ""'''
new_anthropic_check = '''                        # 假成功检测（仅端点启用时）
                        if ep.check_fake_success:
                            _reply_text = reply.strip()
                            if _reply_text and any(p in _reply_text for p in FAKE_SUCCESS_PATTERNS):
                                sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
                                return None, "fake-success: 内容匹配拒绝模式"
                        return o_body, ""'''
if old_anthropic_check not in content:
    print("FAIL: cannot find anthropic fake check")
    sys.exit(1)
content = content.replace(old_anthropic_check, new_anthropic_check, 1)

# 6. OpenAI path: add check_fake_success guard
old_openai_check = '''                        # 假成功检测
                        _content_text = (content or reasoning or "").strip()
                        if _content_text and any(p in _content_text for p in FAKE_SUCCESS_PATTERNS):
                            sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
                            return None, "fake-success: 内容匹配拒绝模式"
                        return body, ""'''
new_openai_check = '''                        # 假成功检测（仅端点启用时）
                        if ep.check_fake_success:
                            _content_text = (content or reasoning or "").strip()
                            if _content_text and any(p in _content_text for p in FAKE_SUCCESS_PATTERNS):
                                sys_log(f"端点 '{ep.name}' 假成功（内容匹配拒绝模式）", "WARNING")
                                return None, "fake-success: 内容匹配拒绝模式"
                        return body, ""'''
if old_openai_check not in content:
    print("FAIL: cannot find openai fake check")
    sys.exit(1)
content = content.replace(old_openai_check, new_openai_check, 1)

# 7. UI: add fFakeCheck select to the form (after fPool)
old_ui_form = '      <div class=\"form-group\"><label>加入聚合池</label><select id=\"fPool\"><option value=\"false\">否（仅保存）</option><option value=\"true\">是（参与轮换）</option></select></div>'
new_ui_form = '      <div class=\"form-group\"><label>加入聚合池</label><select id=\"fPool\"><option value=\"false\">否（仅保存）</option><option value=\"true\">是（参与轮换）</option></select></div>\n      <div class=\"form-group\"><label title=\"检测上游返回内容是否含拒绝信息，从而触发自动轮转\">假成功检测</label><select id=\"fFakeCheck\"><option value=\"false\">关闭</option><option value=\"true\">开启</option></select></div>'
if old_ui_form not in content:
    print("FAIL: cannot find UI form fPool")
    sys.exit(1)
content = content.replace(old_ui_form, new_ui_form, 1)

# 8. JS reset form: add fFakeCheck reset
old_js_reset = "document.getElementById('fVision').value='true';"
new_js_reset = "document.getElementById('fVision').value='true';document.getElementById('fFakeCheck').value='false';"
if old_js_reset not in content:
    print("FAIL: cannot find JS reset fVision")
    sys.exit(1)
content = content.replace(old_js_reset, new_js_reset, 1)

# 9. JS editEndpoint: add fFakeCheck set
old_js_edit = "document.getElementById('fVision').value=String(ep.is_vision!==false);"
new_js_edit = "document.getElementById('fVision').value=String(ep.is_vision!==false);document.getElementById('fFakeCheck').value=String(ep.check_fake_success===true);"
if old_js_edit not in content:
    print("FAIL: cannot find JS edit fVision")
    sys.exit(1)
content = content.replace(old_js_edit, new_js_edit, 1)

# 10. JS saveEndpoint: add fFakeCheck to the payload
old_js_save = "is_vision:document.getElementById('fVision').value==='true',in_pool:document.getElementById('fPool').value==='true'};"
new_js_save = "is_vision:document.getElementById('fVision').value==='true',in_pool:document.getElementById('fPool').value==='true',check_fake_success:document.getElementById('fFakeCheck').value==='true'};"
if old_js_save not in content:
    print("FAIL: cannot find JS save endpoint payload")
    sys.exit(1)
content = content.replace(old_js_save, new_js_save, 1)

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK")
