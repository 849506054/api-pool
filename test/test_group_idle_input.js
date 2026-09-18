// 空闲时间（main 组）输入解析自检：node test_group_idle_input.js
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');

// 弹窗只对 main 组渲染该字段；保存时缺元素按 0（关闭）处理
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }
assert(/const idleEl=document\.getElementById\('gIdle'\);/.test(src), 'savePoolGroup 取 gIdle 需容忍缺失');
assert(/idleEl\?idleEl\.value\.trim\(\):''/.test(src), '缺元素时回退空串');
assert(/g\.is_main\?`<div class="form-group"><label title="main 的当前\/手动工作端点空闲满/.test(src), '空闲时间字段仅 main 弹窗渲染');
assert(/idle_seconds\}=0\)/.test(src) || /idleRaw===''\?0:parseInt\(idleRaw,10\)/.test(src), '空值=0（关闭）');

// 校验：0 或 10–86400
const parse=(raw)=>{const s=String(raw??'').trim();return s===''?0:parseInt(s,10);};
const ok=(v)=>!Number.isNaN(v)&&(v===0||(v>=10&&v<=86400));
assert(ok(parse('')), '空=关闭');
assert(ok(parse('0'))&&ok(parse('10'))&&ok(parse('86400')), '边界值合法');
assert(!ok(parse('9'))&&!ok(parse('86401'))&&!ok(parse('abc')), '越界/非法被拒');

// 请求体携带 idle_seconds（编辑 main 时）
assert(/r=await api\('PUT',`\/api\/groups\/\$\{encodeURIComponent\(editName\)\}`,{name,type,model,context_tokens,idle_seconds}\)/.test(src), 'PUT 携带 idle_seconds');
assert(/r=await api\('POST','\/api\/groups',{name,type,model,context_tokens,idle_seconds}\)/.test(src), 'POST 携带 idle_seconds');
console.log('OK: 空闲时间输入自检通过');
