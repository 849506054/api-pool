#!/usr/bin/env node
// node test/test_subgroup_disabled_ui.js [index.html]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Script} = require('node:vm');
const html = fs.readFileSync(process.argv[2] || path.join(__dirname, '../static/index.html'), 'utf8');
for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new Script(match[1]);
function section(start, end) {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert(a >= 0 && b > a, `source boundaries: ${start}`);
  return html.slice(a, b);
}
const epErrSrc = html.match(/function epErr\(ep\)\{[^\n]*\}/)[0];
const source = epErrSrc + '\n'
  + section("let poolGroupFilter='main';", '\nasync function endpointModels(');
const nodes = {};
const document = {getElementById: id => nodes[id] ||= {innerHTML: '', textContent: ''}};
const window = {};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ui = new Function('document', 'window', 'esc', 'escAttr', 'fmtTime', 'prefetchPoolModels', `
  let poolSnapshot;
  ${source}
  return {renderPoolList, load(data){poolSnapshot=data;window._groupDefs=data.groupDefs;window._poolGroups=data.groups;
    renderPoolList(data.endpoints);}};
`)(document, window, esc, esc, s => `${s}s`, () => {});
const groupDefs = [
  {name:'main', type:'mixed', model:'api-pool'},
  {name:'bg', type:'dedicated', model:'bg', priority_by_group:{main:2}, disabled_groups:['main']},
  {name:'ok', type:'dedicated', model:'ok', priority_by_group:{main:1}},
];
const groups = {main:{current_subgroup:null}, bg:{disabled_groups:['main'], all_frozen_remaining:0, current_endpoint:'b1'},
  ok:{disabled_groups:[], all_frozen_remaining:0, current_endpoint:'o1'}};
ui.load({groupDefs, groups, endpoints:[]});
const out = nodes.poolList.innerHTML;
assert.match(out, /⏸本组停用/, 'disabled subgroup shows 本组停用 badge');
assert.match(out, /toggleSubgroupDisabled\(&quot;bg&quot;, &quot;main&quot;\)">▶</, 'disabled subgroup offers ▶ to re-enable');
assert.match(out, /toggleSubgroupDisabled\(&quot;ok&quot;, &quot;main&quot;\)">⏸</, 'active subgroup offers ⏸ to disable');
assert.match(out, /已在本组停用：不参与 main 组轮转/, 'badge title states scope');
assert.doesNotMatch(out, /switchGroupToSubgroup\(&quot;bg&quot;/, 'no manual switch while disabled');
assert.match(out, /当前端点：—（本组停用）/, 'disabled card explains why no current endpoint');
// 按钮顺序与普通端点卡片一致：优先级 → 停用 → 编辑 → 切换 → 移出
const posToggle = out.indexOf('toggleSubgroupDisabled(&quot;ok&quot;');
const posEdit = out.indexOf('setPoolGroupFilter(&quot;ok&quot;)');
const posSwitch = out.indexOf('switchGroupToSubgroup(&quot;ok&quot;');
assert(posToggle > 0 && posToggle < posEdit && posEdit < posSwitch, 'toggle button sits right after priority');

console.log('subgroup disabled UI ok');
