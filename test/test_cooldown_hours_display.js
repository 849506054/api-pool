// 冷却时长 dhms 展示自检：node test_cooldown_hours_display.js
// 规则（2026-09-19）：dhms 格式，默认只显示最高两个单位（1d4h3m4s → 1d4h）；输入仍以分钟计。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');

function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 1) 展示辅助函数存在且公式正确
assert(/function fmtDhms\(s\)\{/.test(src), 'fmtDhms 定义缺失');
assert(/function fmtCdMin\(m\)\{return fmtDhms\(\(m\|\|0\)\*60\);\}/.test(src), 'fmtCdMin 未走 fmtDhms');
assert(/function fmtCdSec\(s\)\{return fmtDhms\(s\);\}/.test(src), 'fmtCdSec 未走 fmtDhms');

// 2) 配置冷却（❄️）走 fmtCdMin；剩余冷却（⏳）走 fmtCdSec；输入框不变
assert(/❄️\$\{fmtCdMin\(ep\.cooldown_minutes\)\}/.test(src), '配置冷却未改用 fmtCdMin');
assert((src.match(/⏳\$\{fmtCdSec\(ep\.cooldown_remaining\)\}/g)||[]).length===2, '剩余冷却两处未改用 fmtCdSec');
assert(/<label>冷却 \(分钟\)<\/label><input type="number" id="fCooldown"/.test(src), '冷却输入框应仍为分钟');
assert(/cooldown_minutes:Math\.max\(1,parseInt\(document\.getElementById\('fCooldown'\)\.value\)\|\|1\)/.test(src), '提交仍按分钟');

// 3) 复现展示逻辑，锁定边界
const fmtTime=s=>{if(s<=0)return'';if(s<60)return s+'s';const m=Math.floor(s/60);return(s%60)?`${m}m${s%60}s`:`${m}m`;};
const fmtDhms=s=>{s=Math.max(0,Math.floor(s||0));const u=[[Math.floor(s/86400),'d'],[Math.floor(s%86400/3600),'h'],[Math.floor(s%3600/60),'m'],[s%60,'s']];const i=u.findIndex(x=>x[0]>0);if(i<0)return '0s';let r=u[i][0]+u[i][1];if(i+1<u.length&&u[i+1][0]>0)r+=u[i+1][0]+u[i+1][1];return r;};
const fmtCdMin=m=>fmtDhms((m||0)*60);
const fmtCdSec=s=>fmtDhms(s);
assert(fmtDhms(90064)==='1d1h','1d1h3m4s 应只显示两位');
assert(fmtDhms(100984)==='1d4h','1d4h3m4s 应只显示两位');
assert(fmtDhms(288)==='4m48s','4m48s 两位单位');
assert(fmtDhms(45)==='45s','不足一分钟只显示秒');
assert(fmtDhms(3600)==='1h','整小时不补零单位');
assert(fmtDhms(0)==='0s','零时长');
assert(fmtCdMin(59)==='59m','59 分钟应为 59m');
assert(fmtCdMin(60)==='1h','60 分钟应为 1h');
assert(fmtCdMin(90)==='1h30m','90 分钟应为 1h30m');
assert(fmtCdMin(1020)==='17h','1020 分钟应为 17h');
assert(fmtCdSec(55387)==='15h23m','15 小时级冷却应为 15h23m');
assert(fmtCdSec(3600)==='1h','3600s 应为 1h');
assert(fmtCdSec(5400)==='1h30m','5400s 应为 1h30m');
console.log('OK: 冷却时长 dhms 展示自检通过');
