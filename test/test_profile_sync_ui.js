/* 渲染层静态断言：node test/test_profile_sync_ui.js
   直接读 static/index.html，抽出 profile 管理渲染函数在 stub 环境里执行，断言
   「profile 列表每行有 🔄 身份同步按钮：有采样时可用、无采样时禁用；
   点击走 POST /api/client-profiles/sync-identity；同步后有变更/无变更两种提示」。无需浏览器。 */
const fs=require('fs');
const path=require('path');
const src=fs.readFileSync(path.join(__dirname,'..','static','index.html'),'utf8');
const js=src.match(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/)[1];
function grab(name){
  const astart=js.indexOf('async function '+name+'(');
  if(astart>=0)return js.slice(astart,js.indexOf('\n}',astart)+2);
  const start=js.indexOf('function '+name+'(');
  if(start<0)throw new Error('missing function '+name);
  const end=js.indexOf('\n}',start);
  return js.slice(start,end+2);
}

let clientProfiles=[{name:'hermes',builtin:false,headers:{'User-Agent':'hermes-agent/0.21.0','X-Stainless-Lang':'python'}}];
let probeClientProfile='hermes';
let hasClientBaseline=true;
let hermesSample={available:false};
const esc=s=>String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const escAttr=esc;
const profileVersion=h=>{const ua=(h||{})['User-Agent']||'';const m=/^([^/]+)\/(.+)$/.exec(ua);return m?`${m[1]} ${m[2]}`:'';};
const profileOptionLabel=p=>{const v=profileVersion(p.headers);return v?`${p.name}（${v}）`:p.name;};
const els={};
const document={getElementById:id=>els[id]||(els[id]={innerHTML:'',value:''}),body:{appendChild(){}}};
const profileOverlay=()=>els['pm']||(els['pm']={innerHTML:'',className:'',id:'pm'});

let apiCalls=[];
const api=async(method,url,payload)=>{apiCalls.push({method,url,payload});return apiResponses.shift()||{ok:true,changes:[]};};
let apiResponses=[];
let toasts=[];
const toast=(m,t)=>toasts.push({m,t});
const loadClientProfiles=async()=>{};   // 同步成功后前端会重载列表（stub）

eval([grab('renderProfileManager'),grab('syncProfileIdentity')].join('\n'));

let fail=0;
const chk=(c,m)=>{console.log((c?'PASS ':'FAIL ')+m);if(!c)fail=1;};

// 1) 无采样：按钮存在但禁用，title 说明原因
hermesSample={available:false};
renderProfileManager();
chk(/🔄/.test(profileOverlay().innerHTML),'无采样时仍渲染 🔄 按钮');
chk(/disabled/.test(profileOverlay().innerHTML),'无采样时 🔄 禁用');
chk(/暂无 Hermes 流量采样/.test(profileOverlay().innerHTML),'无采样时 tooltip 说明需 Hermes 流量');

// 2) 有采样：按钮可用，title 带版本与来源
hermesSample={available:true,version:'0.21.2',source:'/v1/chat/completions',ts:1};
renderProfileManager();
const html=profileOverlay().innerHTML;
chk(!/disabled/.test(html),'有采样时 🔄 可点');
chk(/0\.21\.2/.test(html)&&/\/v1\/chat\/completions/.test(html),'tooltip 显示采样版本与来源');
chk(/syncProfileIdentity\('hermes'\)/.test(html),'🔄 绑定 syncProfileIdentity(profile 名)');

// 3) 点击同步：命中正确端点，有变更时提示变更项
apiCalls=[];toasts=[];
apiResponses=[{ok:true,changes:[{header:'User-Agent',old:'hermes-agent/0.21.0',new:'hermes-agent/0.21.2'}]}];
(async()=>{
  await syncProfileIdentity('hermes');
  chk(apiCalls.length===1&&apiCalls[0].method==='POST'&&apiCalls[0].url==='/api/client-profiles/sync-identity'
      &&apiCalls[0].payload.name==='hermes','点击走 POST /api/client-profiles/sync-identity');
  chk(toasts.length===1&&toasts[0].t==='success'&&/已更新 1 项/.test(toasts[0].m)&&/User-Agent/.test(toasts[0].m),
      '有变更：提示更新项数与被改头名');

  // 4) 无变更（幂等）与错误分支
  apiCalls=[];toasts=[];apiResponses=[{ok:true,changes:[]}];
  await syncProfileIdentity('hermes');
  chk(toasts.length===1&&/无变更/.test(toasts[0].m),'无变更：提示已是最近值');

  apiCalls=[];toasts=[];apiResponses=[{error:'暂无 Hermes 流量采样（需有一条 UA 为 hermes-agent/<ver> 的请求经过池）'}];
  await syncProfileIdentity('hermes');
  chk(toasts.length===1&&toasts[0].t==='error'&&/暂无 Hermes 流量采样/.test(toasts[0].m),'后端 400：原样提示错误');

  console.log(fail?'PROFILE_SYNC_UI_FAILED':'PROFILE_SYNC_UI_OK');
  process.exit(fail);
})();
