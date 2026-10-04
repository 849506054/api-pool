// 组级日志显示级别（2026-10-05）前端自检：node test_group_log_level.js
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 弹窗渲染三态下拉 + 回填
assert(/<select id="gLogLevel">/.test(src), '组弹窗含日志级别下拉');
assert(/g\.log_level==='error'/.test(src)&&/g\.log_level==='silent'/.test(src), '编辑回填非默认态');
// 保存收集 + PUT/POST 携带
assert(/const log_level=document\.getElementById\('gLogLevel'\)\?document\.getElementById\('gLogLevel'\)\.value:'all';/.test(src), '取值容忍缺失元素');
assert(/context_tokens,idle_seconds,log_level,rotate_minutes\}\)/.test(src), 'PUT/POST 携带 log_level');
// 过滤逻辑存在且被两个显示面调用
assert(/function groupLogVisible\(msg, level\)/.test(src), '实时日志过滤函数');
assert(/if \(!groupLogVisible\(entry\.msg, entry\.level\)\) return;/.test(src), 'addLogLine 应用过滤');
assert(/filter\(l => chatLogVisible\(l\.pool_group\)\)/.test(src), '对话日志列表应用过滤');
assert(/applyGroupLogFilter\(\)/.test(src), '保存后即时清理已渲染行');

// 判定语义模拟
const _LOG_ERROR_LEVELS=new Set(['WARN','WARNING','ERROR']);
function mk(levels){return levels;} // {组名: 级别}
const visible=(m,msg,level)=>{let shown=true;for(const[grp,lv]of Object.entries(m)){if(msg&&msg.includes(`[${grp}]`)){if(lv==='silent')return false;if(lv==='error'&&!_LOG_ERROR_LEVELS.has(level))shown=false;}}return shown;};
assert(visible({},'[main]ep1 收到 API 请求','INFO'), '无级别设置=全显示');
assert(visible({bg:'error'},'[bg]ep1 收到 API 请求','INFO')===false, 'error 级隐藏 INFO 行');
assert(visible({bg:'error'},'[bg]ep1 请求失败: 500','WARN'), 'error 级保留报错行');
assert(visible({bg:'silent'},'[bg]ep1 请求失败','ERROR')===false, 'silent 级全隐藏');
assert(visible({bg:'silent'},'[main]ep1 ok','INFO'), '其他组不受影响');
assert(visible({main:'error'},'全局日志无组标签','INFO'), '无组标签行不受影响');
console.log('OK: 组日志级别前端自检通过');
