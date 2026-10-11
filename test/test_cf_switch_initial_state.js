// 敏感词过滤弹窗开关初始态自检：node test_cf_switch_initial_state.js
// 口径：开关类名与状态文案必须由接口返回值（cf.enabled）渲染，写死默认态＝每次重开都错。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 源码护栏：模板里不再出现写死的初始态
assert(!/class="cf-switch on"/.test(src), '开关初始类名不再写死 on');
assert(!/class="cf-status on">已开启/.test(src), '状态文案不再写死「已开启」');

// 渲染断言：按模板实渲染开启/关闭两态，写死写法会立刻变红
const i=src.indexOf('function renderCFManager()');
assert(i>=0, '找到 renderCFManager');
const j=src.indexOf('overlay.innerHTML=`',i)+'overlay.innerHTML=`'.length;
const tpl=src.slice(j,src.indexOf('`;',j));
function render(enabled){
  const cf={enabled,rules:[{type:'literal',pattern:'x',replacement:''}],dictionary_version:'v1'};
  const esc=s=>String(s);
  return new Function('cf','esc','return `'+tpl+'`;')(cf,esc);
}
const on=render(true), off=render(false);
assert(/class="cf-switch on" id="cfSwitch"/.test(on), '开启态：开关带 on');
assert(/class="cf-status on" id="cfStatus">已开启</.test(on), '开启态：文案「已开启」');
assert(/class="cf-switch" id="cfSwitch"/.test(off), '关闭态：开关不带 on');
assert(/class="cf-status off" id="cfStatus">已关闭</.test(off), '关闭态：文案「已关闭」');
assert(!/已开启/.test(off), '关闭态不出现「已开启」');
console.log('OK: 敏感词过滤弹窗开关初始态自检通过');
