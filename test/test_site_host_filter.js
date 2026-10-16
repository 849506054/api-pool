// 站点筛选 host 收拢自检（零依赖）
const assert=require('assert'), fs=require('fs');
const page=fs.readFileSync('/opt/data/work/api-pool2/static/index.html','utf8')
  .match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

function extract(name){
  const i=page.indexOf('function '+name+'(');
  if(i<0) throw new Error('not found: '+name);
  let d=0,st=false;
  for(let j=i;j<page.length;j++){
    if(page[j]==='{'){d++;st=true;}
    else if(page[j]==='}'){d--;if(st&&d===0) return page.slice(i,j+1);}
  }
  throw new Error('unbalanced: '+name);
}
// 生产真实形状：同站多 key 副本（Cline 4 个 site_name 同一 host）
const eps=[
  {name:'Cline-ds4.1f',site_name:'Cline',base_url:'https://api.cline.bot/api/v1',model:'cline-free/deepseek-v4.1-flash'},
  {name:'Cline2-ds4.1f',site_name:'Cline2',base_url:'https://api.cline.bot/api/v1',model:'cline-free/deepseek-v4.1-flash'},
  {name:'Cline3-ds4.1f',site_name:'Cline3',base_url:'https://api.cline.bot/api/v1',model:'cline-free/deepseek-v4.1-flash'},
  {name:'Cline4-ds4.1f',site_name:'Cline4',base_url:'https://api.cline.bot/api/v1',model:'cline-free/deepseek-v4.1-flash'},
  {name:'AgentRouter-opus',site_name:'AgentRouter',base_url:'https://ps.air-outer.com/v1',model:'claude-opus-4-8'},
  {name:'AgentRouterZ',site_name:'AgentRouterZ',base_url:'https://ps.air-outer.com/v1',model:'gpt-5.6-sol'},
  {name:'Tokenrhythm',site_name:'Tokenrhythm',base_url:'https://tokenrhythm.studio/v1',model:'glm-5.3'},
  // 内网网关：同 host 下 site_name 无公共前缀（7864）与只差大小写（8790）
  {name:'WorkBuddy-cron',site_name:'WorkBuddy',base_url:'http://192.168.5.6:7864/v1',model:'glm-5.3'},
  {name:'WorkBuddy-free',site_name:'WorkBuddy',base_url:'http://192.168.5.6:7864/v1',model:'glm-5.3'},
  {name:'CodeBuddy',site_name:'CodeBuddy',base_url:'http://192.168.5.6:7864/v1',model:'glm-5.3'},
  {name:'Qoder-cron',site_name:'Qoder',base_url:'http://192.168.5.6:8790/v1',model:'qwen3.8-flash'},
  {name:'qoderCN-qwen3.8f',site_name:'qoderCN',base_url:'http://192.168.5.6:8790/v1',model:'qwen3.8-flash'},
];

// stub：页面依赖的浏览器对象/函数
const els={};
function mkEl(id){ return els[id]||(els[id]={id,innerHTML:'',textContent:'',scrollTop:0}); }
global.document={getElementById:mkEl};
global.window={};
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}));
const escAttr=esc;
const vendorOf=m=>{ if(/claude|opus/i.test(m))return 'Anthropic'; if(/gpt/i.test(m))return 'OpenAI'; if(/glm/i.test(m))return 'GLM'; return '其他'; };
const ABN_FILTER='__abnormal__';
let epFilter='all',epVendorFilter='all',modelPage=1,PP=50;
function setFilter(f){epFilter=f;}
function setVendorFilter(v){epVendorFilter=v;}
const epErr=()=>false;

const ctx={document:global.document,window:global.window,esc,escAttr,vendorOf,ABN_FILTER,
  epFilter,epVendorFilter,modelPage,PP,setFilter,setVendorFilter,epErr,
  get epFilter(){return epFilter;}, set epFilter(v){epFilter=v;},
  get epVendorFilter(){return epVendorFilter;}, set epVendorFilter(v){epVendorFilter=v;},
};
const src=[extract('epHost'),extract('epSiteLabel'),extract('epSiteAlias'),extract('renderFilterBar')].join('\n');
const run=new Function('ctx', 'with(ctx){'+src+'\nreturn {renderFilterBar,epHost,epSiteLabel};}')(ctx);

// 断言 1：host 归拢（Cline×4 → 1 个站点标签）
run.renderFilterBar(eps);
const fb=mkEl('filterBar').innerHTML;
assert.ok(/>Cline 4</.test(fb),'Cline4 条应收拢成 1 个标签并显示短名: '+fb);
assert.ok(!fb.includes('>Cline2<')&&!fb.includes('>Cline3<')&&!fb.includes('>Cline4<'),'不应再出现 Cline2/3/4 标签');
assert.ok(/>AgentRouter 2</.test(fb),'AgentRouter 两条应收拢');
// 断言 2：「全部站点」= 站点数（5），不是端点数（12）
assert.ok(/全部站点\s+5</.test(fb),'全部站点应显示站点数 5，实际: '+(fb.match(/全部站点[^<]*/)||[''])[0]);
// 断言 3：「全部厂商」= 厂商数，不是端点数
const vb=mkEl('vendorBar').innerHTML;
assert.ok(/全部厂商\s+\d+</.test(vb)&&!/全部厂商\s+7</.test(vb),'全部厂商不应显示端点数 7，实际: '+(vb.match(/全部厂商[^<]*/)||[''])[0]);
// 断言 3b：标签显示短公共前缀，不再出现 Cline2/3/4
const srcA=extract('epSiteAlias');
const alias=new Function('ctx','with(ctx){'+extract('epHost')+'\n'+srcA+'\nreturn epSiteAlias;}')(Object.assign({},{epHost:run.epHost}));
assert.strictEqual(alias(eps,'api.cline.bot'),'Cline','Cline 一家应显示 Cline，而非 Cline/2/3/4');
assert.strictEqual(alias(eps,'ps.air-outer.com'),'AgentRouter','AgentRouter 一家应显示 AgentRouter');
assert.ok(!/>Cline2</.test(fb)&&!/>Cline3</.test(fb)&&!/>Cline4</.test(fb),'筛选栏不应再出现 Cline2/3/4 标签');
assert.ok(/>Cline 4</.test(fb),'应显示「Cline 4」，实际: '+(fb.match(/Cline[^<]*/)||[''])[0]);
// 断言 3c（2026-10-16）：前缀比较大小写不敏感（Qoder/qoderCN 只差大小写 → Qoder）
assert.strictEqual(alias(eps,'192.168.5.6:8790'),'Qoder','只差大小写的一站应取到公共前缀 Qoder');
// 断言 3d（2026-10-16）：无公共前缀时并列显示全部 site_name（端点数多的在前），不回落 ip:port
assert.strictEqual(alias(eps,'192.168.5.6:7864'),'WorkBuddy/CodeBuddy','无公共前缀的一站应并列显示名字');
assert.ok(!/>192\.168\.5\.6:7864</.test(fb)&&!/>192\.168\.5\.6:8790</.test(fb),'筛选栏不应出现 ip:port 标签');
assert.ok(/>WorkBuddy\/CodeBuddy 3</.test(fb),'应显示「WorkBuddy/CodeBuddy 3」，实际: '+(fb.match(/WorkBuddy[^<]*/)||[''])[0]);
assert.ok(/>Qoder 2</.test(fb),'应显示「Qoder 2」，实际: '+(fb.match(/Qoder[^<]*/)||[''])[0]);
console.log('site-host filter self-check: OK  (全部站点=5, Cline 收拢为 Cline 4, 无前缀站并列名字)');

// 断言 4：选中某 host 后，renderEndpoints 只渲染该站端点
const srcE=extract('renderEndpoints');
mkEl('epList').innerHTML='';
const ctx2={document:global.document,esc,escAttr,vendorOf,ABN_FILTER,
  hBadge:()=>'',muInfo:()=>({}),fmtCdSec:()=>'',fmtTime:()=>'',timeAgo:()=>'',fmtCdMin:()=>'',
  wkmCreditBadge:()=>'', /* 积分徽标：本用例只验站点筛选/别名，徽标内容另有 render_error_smoke.js 覆盖 */
  clientProfiles:[],profileVersion:()=>'',siteUrl:()=>'',epErr:()=>false,
  get epFilter(){return epFilter;}, set epFilter(v){epFilter=v;},
  get epVendorFilter(){return epVendorFilter;}, set epVendorFilter(v){epVendorFilter=v;},
  modelPage:1,PP:50,
};
const render=new Function('ctx','with(ctx){'+srcE+'\nreturn renderEndpoints;}')(Object.assign(ctx2,{epHost:run.epHost}));
epFilter='api.cline.bot';
render(eps);
const n=(mkEl('epList').innerHTML.match(/class="ep-item/g)||[]).length;
assert.strictEqual(n,4,'选中 api.cline.bot 后应渲染 4 条 Cline 端点，实际 '+n);
epFilter='tokenrhythm.studio';
render(eps);
const n2=(mkEl('epList').innerHTML.match(/class="ep-item/g)||[]).length;
assert.strictEqual(n2,1,'选中 tokenrhythm 后应渲染 1 条端点，实际 '+n2);
console.log('filter-by-host: OK (cline=4, tokenrhythm=1)');
