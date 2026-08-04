#!/usr/bin/env python3
"""Add mobile-responsive CSS/JS improvements to api_pool_server.py"""
import sys

path = "/vol1/1000/tool/api-pool/api_pool_server.py"

with open(path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add comprehensive mobile CSS after the one existing @media rule
old_media = "@media(max-width:920px){.grid{grid-template-columns:1fr}}"
new_media = """@media(max-width:920px){.grid{grid-template-columns:1fr}}
/* ── 移动端适配 ── */
@media(max-width:768px){
  body{padding:12px 10px;font-size:13px}
  .header h1{font-size:16px}
  .header h1 .logo{width:26px;height:26px;font-size:13px}
  .header-actions .btn{font-size:11px;padding:5px 10px}
  .dash-stats{grid-template-columns:repeat(2,1fr);gap:10px}
  .dash-stat{padding:14px}
  .dash-stat .num{font-size:18px}
  .dash-stat .label{font-size:10px}
  .stats{grid-template-columns:repeat(3,1fr);gap:6px}
  .stat-item{padding:10px 6px}
  .stat-item .num{font-size:16px}
  .ep-item{padding:10px}
  .ep-header{flex-direction:column;align-items:flex-start;gap:6px}
  .ep-actions{flex-wrap:wrap;gap:3px;width:100%}
  .ep-actions .btn{font-size:10px;padding:3px 7px;min-width:26px;text-align:center}
  .ep-meta{flex-wrap:wrap;gap:4px;font-size:10px}
  .ep-meta span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:45%}
  .ep-name{font-size:13px;width:100%}
  .ep-name .badge,.ep-name span.badge{font-size:9px;padding:2px 5px}
  .ep-error{font-size:10px;padding:4px 6px}
  .chain-item{padding:10px}
  .card{padding:12px 14px}
  .card-title{font-size:12px}
  .filter-bar{flex-wrap:wrap;gap:4px}
  .filter-bar .filter-btn{font-size:11px;padding:3px 8px}
  .form-row{grid-template-columns:1fr!important;gap:8px}
  .form-group{margin-bottom:0}
  .form-group label{font-size:11px;margin-bottom:2px}
  .form-group input,.form-group select{font-size:13px;padding:6px 8px}
  .modal{width:95%;padding:16px;max-height:90vh;overflow-y:auto}
  .model-row{flex-direction:column;align-items:stretch;gap:6px}
  .model-row .btn{width:100%}
  .api-info-card{padding:10px 12px;font-size:12px}
  .api-info-card code{font-size:11px}
  .test-drawer{width:95%;right:2.5%}
  .test-drawer .drawer-header{flex-direction:column;align-items:flex-start;gap:6px}
  .test-drawer .drawer-header .btn{width:100%}
  .log-header{flex-wrap:wrap;gap:6px}
  .log-header select{width:100%}
  .log-row{flex-direction:column;gap:4px;padding:8px}
  .log-row .log-time{font-size:10px;min-width:auto}
  .log-row .log-msg{font-size:11px}
  .log-row .log-level{font-size:10px;min-width:30px}
  .seg-ctrl .seg-btn{font-size:10px;padding:3px 8px}
  .stats-ttl{font-size:12px;padding:10px 0 6px}
  .stats-ttl .num{font-size:16px}
  .stats-ttl .label{font-size:10px}
  table{font-size:10px!important}
  table th,table td{padding:4px!important}
  .chat-log-item{font-size:11px;padding:6px 8px}
  .chat-log-meta{font-size:10px;flex-wrap:wrap}
  .toast{font-size:12px;padding:8px 16px;bottom:16px;left:10px;right:10px;width:auto}
  .modal-overlay .modal .form-actions{flex-direction:column;gap:6px}
  .modal-overlay .modal .form-actions .btn{width:100%}
}
@media(max-width:480px){
  body{padding:8px 6px;font-size:12px}
  .dash-stats{grid-template-columns:1fr 1fr;gap:6px}
  .dash-stat{padding:10px}
  .dash-stat .num{font-size:16px}
  .stats{grid-template-columns:repeat(3,1fr);gap:4px}
  .stat-item{padding:8px 4px;border-radius:8px}
  .stat-item .num{font-size:14px}
  .stat-item .label{font-size:9px}
  .header h1{font-size:14px}
  .header-actions{gap:4px}
  .header-actions .btn{font-size:10px;padding:4px 8px}
  .ep-actions .btn{font-size:9px;padding:2px 5px;min-width:22px}
  .modal{width:100%;padding:12px;border-radius:12px}
  .card{padding:10px 12px;border-radius:12px}
  .ep-meta span{max-width:100%}
  .filter-bar{gap:3px}
  .filter-bar .filter-btn{font-size:10px;padding:2px 6px}
  .api-info-card{padding:8px 10px;font-size:11px}
  .api-info-card code{font-size:10px}
  .chain-item{padding:8px;font-size:12px}
  .chain-item .chain-dot{width:8px;height:8px}
  .test-drawer{width:98%;right:1%;border-radius:12px;padding:12px}
  .test-drawer textarea{font-size:12px;min-height:60px}
  .chat-log-item{font-size:10px;padding:4px 6px}
  .chat-log-meta{font-size:9px}
  .log-row{padding:6px}
  .log-row .log-msg{font-size:10px}
  .badge{font-size:8px;padding:1px 4px}
  .ep-name .badge,.ep-name span.badge{font-size:8px;padding:1px 4px}
  .btn{padding:5px 10px;font-size:11px}
  .btn-sm{padding:2px 6px;font-size:10px}
}
@media(max-width:380px){
  .dash-stats{grid-template-columns:1fr;gap:4px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .ep-actions{gap:2px}
  .ep-actions .btn{font-size:8px;padding:2px 4px;min-width:20px}
}"""

if old_media not in content:
    print("FAIL: cannot find @media rule")
    sys.exit(1)
content = content.replace(old_media, new_media, 1)

# 2. Add mobile-friendly touch-action to buttons (avoid 300ms tap delay)
old_btn_style = ".btn{padding:7px 14px;border:none;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;transition:all .12s;display:inline-flex;align-items:center;gap:5px;letter-spacing:.2px}"
new_btn_style = ".btn{padding:7px 14px;border:none;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;transition:all .12s;display:inline-flex;align-items:center;gap:5px;letter-spacing:.2px;touch-action:manipulation;-webkit-tap-highlight-color:transparent}"
if old_btn_style not in content:
    print("FAIL: cannot find .btn style")
    sys.exit(1)
content = content.replace(old_btn_style, new_btn_style, 1)

# 3. Add overflow-x:auto to table containers for horizontal scroll
old_table_wrap = '<div style="max-height: 250px; overflow-y: auto;">'
new_table_wrap = '<div style="max-height: 250px; overflow-y: auto; -webkit-overflow-scrolling: touch;">'
content = content.replace(old_table_wrap, new_table_wrap)

# 4. Add meta viewport with user-scalable=no for better mobile form handling
old_viewport = '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
new_viewport = '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">'
if old_viewport not in content:
    print("FAIL: cannot find viewport meta")
    sys.exit(1)
content = content.replace(old_viewport, new_viewport, 1)

# 5. Add an extra CSS rule for modal form-row to use 1fr on mobile (override inline style)
# The inline style="grid-template-columns: 1fr 1fr 1fr;" on the last form-row needs media override
# Already handled by .form-row{grid-template-columns:1fr!important} in the media query above

with open(path, "w", encoding="utf-8") as f:
    f.write(content)

print("OK")