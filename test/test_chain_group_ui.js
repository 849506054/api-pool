#!/usr/bin/env node
// node test/test_chain_group_ui.js [index.html] [API snapshot JSON]
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
// epErr 是顶层异常判据（渲染函数之外），切片取不到，单独摘出拼进沙箱
const epErrSrc = html.match(/function epErr\(ep\)\{[^\n]*\}/)[0];
const source = epErrSrc + '\n'
  + section("let poolGroupFilter='main';", '\nasync function endpointModels(')
  + section("let chainGroupFilter='main';", '\nasync function runHealthCheck(');
const nodes = {};
const document = {getElementById: id => nodes[id] ||= {innerHTML: '', textContent: ''}};
const window = {};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ui = new Function('document', 'window', 'esc', 'escAttr', 'fmtTime', `
  let poolSnapshot;
  ${source}
  return {setPoolGroupFilter, setChainGroupFilter, renderChain,
    get pool(){return poolGroupFilter}, get chain(){return chainGroupFilter},
    load(data){poolSnapshot=data;window._groupDefs=data.groupDefs;window._poolGroups=data.groups;
      renderPoolList(data.endpoints);renderChain(data.chain);}};
`)(document, window, esc, esc, s => `${s}s`);
const badges = id => [...nodes[id].innerHTML.matchAll(/badge-priority">#(\d+)<\/span>/g)].map(m => Number(m[1]));
const chainNames = () => [...nodes.chainList.innerHTML.matchAll(/badge-priority">#\d+<\/span> ([^<]+) /g)].map(m => m[1].trim());
const ep = (name, priority, groupPriority, groups=['main','g']) => ({
  id:name, name, model:'model', enabled:true, in_pool:true, is_vision:false,
  priority, priority_by_group:groupPriority, pool_groups:groups, current_groups:[]
});
// Explicit test data: global order opposes g, and main also differs from global.
const endpoints = [ep('A',1,{main:2,g:3}), ep('B',2,{main:1,g:1}), ep('C',3,{main:3,g:2}),
  {...ep('disabled',1,{off:1},['off']),enabled:false}];
const data = {endpoints, chain:endpoints.filter(e=>e.enabled), groups:{},
  groupDefs:['main','vision','g','empty','off'].map(name=>({name}))};
const original = JSON.stringify(data);
ui.load(data);
ui.setChainGroupFilter('g');
assert.deepEqual(chainNames(), ['B','C','A']);
assert.deepEqual(badges('chainList'), [1,2,3]);
ui.setPoolGroupFilter('g');
assert.equal(ui.pool, 'g'); assert.equal(ui.chain, 'g');
assert.deepEqual(badges('poolList'), badges('chainList'));
const poolHtml = nodes.poolList.innerHTML;
ui.setChainGroupFilter('main');
assert.equal(ui.pool, 'g'); assert.equal(nodes.poolList.innerHTML, poolHtml);
assert.deepEqual(chainNames(), ['B','A','C']);
ui.load(data); // automatic data redraw preserves independent chain selection
assert.equal(ui.chain, 'main'); assert.equal(ui.pool, 'g');
for (const group of ['empty','off','vision']) {
  ui.setPoolGroupFilter(group);
  assert.equal(ui.chain, group);
  assert(nodes.groupSummary.innerHTML.includes(`class="filter-btn active" onclick="setChainGroupFilter('${group}')"`));
  assert(nodes.chainList.innerHTML.includes('该组没有启用的端点'));
}
ui.setChainGroupFilter('deleted');
assert.equal(ui.chain, 'main');
assert(nodes.groupSummary.innerHTML.includes(`class="filter-btn active" onclick="setChainGroupFilter('main')"`));
assert.deepEqual(badges('chainList'), [1,2,3]);
assert.equal(JSON.stringify(data), original, 'rendering must not mutate API snapshots');
const legacy = [ep('late',9,undefined,[]), ep('early',2,undefined,[])];
ui.load({endpoints:legacy,chain:legacy,groups:{},groupDefs:[]});
ui.setPoolGroupFilter('main');
assert.deepEqual(chainNames(), ['early','late']);
assert.deepEqual(badges('chainList'), [2,9]);
assert.deepEqual(legacy.map(e=>e.name), ['late','early']);
console.log('PASS: script syntax; group ordering/badges; one-way local sync; refresh; empty/disabled/vision groups; invalid-group fallback; legacy fields; immutable snapshots');
if (process.argv[3]) {
  const live = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  ui.load(live);
  for (const {name:g} of live.groupDefs) {
    ui.setPoolGroupFilter(g);
    assert.equal(ui.chain, g);
    const prio=e=>e.priority_by_group?.[g] ?? e.priority;
    const expected=live.endpoints.filter(e=>e.in_pool&&e.enabled&&(e.pool_groups?.length?e.pool_groups:['main']).includes(g))
      .sort((a,b)=>prio(a)-prio(b));
    assert.deepEqual(chainNames(), expected.map(e=>esc(e.name)), g);
    assert.deepEqual(badges('chainList'), expected.map(prio), g);
    console.log(`PASS live ${g}: ${expected.map(e=>`#${prio(e)} ${e.name}`).join(' → ')}`);
  }
}
