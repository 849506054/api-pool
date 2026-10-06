// 组级日志显示级别（2026-10-05）前端自检：node test_group_log_level.js
// 口径：级别过滤在服务端完成（应隐藏的行不下发），前端只渲染服务端给到的行。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 弹窗下拉四档 + 回填
assert(/<select id="gLogLevel">/.test(src), '组弹窗含日志级别下拉');
for (const lv of ['all','error','live','silent']) {
  assert(new RegExp(`<option value="${lv}"`).test(src), `含 ${lv} 档`);
}
assert(/g\.log_level==='live'/.test(src), '编辑回填 live 档');
// 保存收集 + PUT/POST 携带
assert(/const log_level=document\.getElementById\('gLogLevel'\)\?document\.getElementById\('gLogLevel'\)\.value:'all';/.test(src), '取值容忍缺失元素');
assert(/context_tokens,idle_seconds,log_level,rotate_requests,rotate_members\}\)/.test(src), 'PUT/POST 携带 log_level');
// 可见性判定只在服务端：前端无过滤函数，直接采用服务端结果
assert(!/groupLogVisible|chatLogVisible|_groupLogLevels|applyGroupLogFilter/.test(src), '前端不含可见性过滤函数');
assert(/function addLogLine\(entry\) \{\n    if \(!logContainer\) return;\n    const d = document\.createElement\('div'\);/.test(src), 'addLogLine 直接渲染服务端行');
assert(/currentChatLogs = \(res\.logs \|\| \[\]\);/.test(src), '对话日志列表直接采用服务端结果');
// 级别变更后由服务端按新级别重新过滤
assert(/function refreshLogViews\(\)/.test(src), '重置刷新函数存在');
assert(/closeGroupModal\(\);refresh\(\);refreshLogViews\(\);/.test(src), '保存分组后清空并重拉');
console.log('OK: 组日志级别前端自检通过');
