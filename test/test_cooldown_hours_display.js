// 冷却时长小时展示自检：node test_cooldown_hours_display.js
// 规则（2026-09-19）：冷却时长 ≥60 分钟显示为 X.XH；<60 分钟维持原样；输入仍以分钟计。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');

function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 1) 展示辅助函数存在且公式正确
assert(/function fmtCdMin\(m\)\{return m>=60\?\(m\/60\)\.toFixed\(1\)\+'H':m\+'分';\}/.test(src), 'fmtCdMin 定义缺失/公式不符');
assert(/function fmtCdSec\(s\)\{return s>=3600\?\(s\/3600\)\.toFixed\(1\)\+'H':fmtTime\(s\);\}/.test(src), 'fmtCdSec 定义缺失/公式不符');

// 2) 配置冷却（❄️）走 fmtCdMin；剩余冷却（⏳）走 fmtCdSec；输入框不变
assert(/❄️\$\{fmtCdMin\(ep\.cooldown_minutes\)\}/.test(src), '配置冷却未改用 fmtCdMin');
assert((src.match(/⏳\$\{fmtCdSec\(ep\.cooldown_remaining\)\}/g)||[]).length===2, '剩余冷却两处未改用 fmtCdSec');
assert(/<label>冷却 \(分钟\)<\/label><input type="number" id="fCooldown"/.test(src), '冷却输入框应仍为分钟');
assert(/cooldown_minutes:Math\.max\(1,parseInt\(document\.getElementById\('fCooldown'\)\.value\)\|\|1\)/.test(src), '提交仍按分钟');

// 3) 复现展示逻辑，锁定边界
const fmtTime=s=>{if(s<=0)return'';if(s<60)return s+'s';const m=Math.floor(s/60);return(s%60)?`${m}m${s%60}s`:`${m}m`;};
const fmtCdMin=m=>m>=60?(m/60).toFixed(1)+'H':m+'分';
const fmtCdSec=s=>s>=3600?(s/3600).toFixed(1)+'H':fmtTime(s);
assert(fmtCdMin(59)==='59分','59 分钟应保持分');
assert(fmtCdMin(60)==='1.0H','60 分钟应转小时');
assert(fmtCdMin(90)==='1.5H','90 分钟应为 1.5H');
assert(fmtCdMin(1020)==='17.0H','1020 分钟应为 17.0H');
assert(fmtCdSec(3599)===fmtTime(3599),'3599s 维持原样');
assert(fmtCdSec(3600)==='1.0H','3600s 应转小时');
assert(fmtCdSec(5400)==='1.5H','5400s 应为 1.5H');
console.log('OK: 冷却时长小时展示自检通过');
