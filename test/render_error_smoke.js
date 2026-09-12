/* 渲染层静态断言：node test/render_error_smoke.js
   直接读取 static/index.html，抽出渲染函数在 stub 环境里执行，断言
   「异常口径四处统一（epErr = health bad || last_error）：状态卡计数/异常筛选、
   端点列表红框、聚合池红框、聚合链红块；错误原文完整显示」。无需浏览器。 */
const fs=require('fs');
const path=require('path');
const src=fs.readFileSync(path.join(__dirname,'..','static','index.html'),'utf8');
const js=src.match(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/)[1];
function grab(name){
  const start=js.indexOf('function '+name+'(');
  if(start<0)throw new Error('missing function '+name);
  const end=js.indexOf('\n}',start);           // 顶层函数：列 0 的收尾 }
  if(end<0)throw new Error('unterminated function '+name);
  return js.slice(start,end+2);
}

const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const escAttr=esc;
const hBadge=(h,lat)=>{const m={ok:'OK',slow:'SLOW',bad:'BAD',unknown:'UNK',testing:'TEST'};return '['+(m[h]||m.unknown)+']';};
const fmtTime=s=>s+'s';
const timeAgo=()=>'ago';
const siteUrl=u=>u;
const clientProfiles=[];const profileVersion=()=>'';
const ABN_FILTER='__abnormal__';
let epFilter='all';
const els={};
const document={getElementById:id=>els[id]||(els[id]={innerHTML:'',textContent:'',scrollTop:0}),createElement:()=>({style:{},classList:{add(){}},appendChild(){},remove(){}}),body:{appendChild(){}}};
const window={_poolGroups:{},_groupDefs:[],_lastChain:[]};
let poolSnapshot={endpoints:[],chain:[],groups:{},groupDefs:[]};
let poolGroupFilter='main',chainGroupFilter='main';
const setChainGroupFilter=g=>{chainGroupFilter=g;};
const GROUP_ICONS={main:'M',vision:'V'};
const groupIcon=g=>GROUP_ICONS[g]||'';
const groupRank=g=>g==='main'?0:(GROUP_ICONS[g]?1:2);
const sortGroups=list=>list.sort((a,b)=>groupRank(a)-groupRank(b));
const FNS=[js.match(/function epErr\(ep\)\{[^\n]*\}/)[0],grab('renderStats'),grab('renderEndpoints'),grab('renderPoolList'),grab('renderChain')].join('\n');
eval(FNS);

const ERR='HTTP 402: {"error":{"message":"Budget pool quota has been exhausted. Please ask an administrator to raise it."}}';
const ERR_HTML=ERR.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const ep={id:'e1',name:'AgentRouterP-gpt',model:'gpt-5.6-sol',enabled:true,in_pool:true,pool_groups:['main'],
  priority:2,priority_by_group:{main:2},is_current:false,current_groups:[],in_cooldown:false,cooldown_remaining:0,
  cooldown_minutes:5,manual_unlock_required:false,is_deferred:false,defer_remaining:0,health:'bad',
  health_latency_ms:-1,health_error:ERR,last_error:'',timeout:120,max_retries:2,max_context_k:0,total_calls:0,
  last_success:0,health_mode:'chat',billing_mode:'',protocol:'openai',base_url:'http://x',use_proxy:false,is_vision:true};
const epOk=Object.assign({},ep,{id:'e2',name:'Healthy',health:'ok',health_error:'',health_latency_ms:123});
const chainItem=Object.assign({},ep,{fail_count:0,deferrable:true});
const realFail=Object.assign({},chainItem,{name:'RealFail',health:'unknown',health_error:'',last_error:'HTTP 500: upstream boom'});
const epUnk=Object.assign({},ep,{id:'e3',name:'Soleapi',health:'unknown',health_error:'',last_error:'HTTP 404: not found'});
let fail=0;
const chk=(c,m)=>{console.log((c?'PASS ':'FAIL ')+m);if(!c)fail=1;};
renderStats([ep,epOk,epUnk]);
chk(/>2<\/div><div class="label">异常<\/div>/.test(els['stats'].innerHTML),'状态卡：异常计数=epErr 口径（bad + last_error）= 2');
renderPoolList([ep,epOk,epUnk]);
const pool=els['poolList'].innerHTML;
chk((pool.match(/class="ep-item[^"]*has-error/g)||[]).length===2,'聚合池卡：health bad 与 last_error 端点都红框');
chk(!pool.includes('[BAD]')&&!pool.includes('ep-error'),'聚合池卡：不显示健康徽章与错误详情（仅边框高亮）');
renderEndpoints([ep,epOk,epUnk]);
const lst=els['epList'].innerHTML;
chk((lst.match(/class="ep-item[^"]*has-error/g)||[]).length===2,'端点列表：health bad 与 last_error 端点都高亮');
chk(lst.includes('⚠ '+ERR_HTML),'端点列表：last_error 空时回落到 health_error');
renderChain([chainItem,realFail]);
const chain=els['chainList'].innerHTML;
chk((chain.match(/chain-item failed/g)||[]).length===2,'聚合链：错误端点红块（health bad + last_error 都算）');
chk(chain.includes('⚠ '+ERR_HTML),'聚合链：完整 health_error 全文');
chk(chain.includes('⚠ HTTP 500: upstream boom'),'聚合链：last_error 兜底（真实请求失败）');
chk((chain.split('chain-err').length-1)===2,'聚合链：两条错误行');
chk(!/<div class="chain-right">[^<]*<div class="chain-err"/.test(chain),'聚合链：错误详情已移出右列');
chk(!/max-width:120px/.test(src),'CSS：旧的单行省略已移除');
console.log(fail?'\n有断言失败':'\n全部断言通过');
process.exit(fail);
