#!/usr/bin/env node
// node test/test_subgroup_unfreeze_ui.js [index.html]
// 子组条目在父组内冻结时的 ⏰ 解除整组冻结：按钮存在性、位置，及批量解冻行为。
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

// ---- 渲染：冻结的子组条目要出现 ⏰，并指到整组解冻 ----
const epErrSrc = html.match(/function epErr\(ep\)\{[^\n]*\}/)[0];
const source = epErrSrc + '\n' + section("let poolGroupFilter='main';", '\nasync function endpointModels(');
const nodes = {};
const document = {getElementById: id => nodes[id] ||= {innerHTML: '', textContent: ''}};
const window = {};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ui = new Function('document', 'window', 'esc', 'escAttr', 'fmtTime', 'fmtCdSec', 'prefetchPoolModels', `
  let poolSnapshot;
  ${source}
  return {renderPoolList, load(data){poolSnapshot=data;window._groupDefs=data.groupDefs;window._poolGroups=data.groups;
    renderPoolList(data.endpoints);}};
`)(document, window, esc, esc, s => `${s}s`, s => `${s}s`, () => {});
const groupDefs = [
  {name:'main', type:'mixed', model:'api-pool'},
  {name:'bg', type:'dedicated', model:'bg', priority_by_group:{main:1}},
  {name:'ok', type:'dedicated', model:'ok', priority_by_group:{main:2}},
];
const frozenOut = (() => {
  const groups = {main:{current_subgroup:'bg'}, bg:{all_frozen_remaining:180, current_endpoint:null}, ok:{all_frozen_remaining:0, current_endpoint:'o1'}};
  ui.load({groupDefs, groups, endpoints:[]});
  return nodes.poolList.innerHTML;
})();
assert.match(frozenOut, /⏳180s/, 'frozen subgroup shows its cooldown badge');
assert.match(frozenOut, /onclick="clearSubgroupCooldown\(&quot;bg&quot;\)">⏰</, 'frozen subgroup offers the unfreeze button');
assert.match(frozenOut, /与端点卡片 ⏰ 同口径/, 'button title states the shared semantics');
const posBadge = frozenOut.indexOf('⏳180s');
const posBtn = frozenOut.indexOf('clearSubgroupCooldown(&quot;bg&quot;)');
assert(posBadge > 0 && posBtn > posBadge, 'unfreeze button sits right after the cooldown badge');
assert.doesNotMatch(frozenOut, /clearSubgroupCooldown\(&quot;ok&quot;\)/, 'non-frozen subgroup has no unfreeze button');

const warmOut = (() => {
  const groups = {main:{current_subgroup:null}, bg:{all_frozen_remaining:0, current_endpoint:'b1'}, ok:{all_frozen_remaining:0, current_endpoint:'o1'}};
  ui.load({groupDefs, groups, endpoints:[]});
  return nodes.poolList.innerHTML;
})();
assert.doesNotMatch(warmOut, /clearSubgroupCooldown/, 'no unfreeze button while the subgroup is not frozen');

// ---- 行为：只解冻冻结成员，逐个走既有 clear-error ----
const fnSrc = section('async function clearSubgroupCooldown', 'async function setSubgroupPriority');
async function run(snapshot, responder) {
  const calls = [], toasts = [];
  let refreshed = 0;
  const api = async (method, url) => { calls.push(`${method} ${url}`); return responder(url); };
  const fn = new Function('api', 'refresh', 'toast', 'poolSnapshot', 'encodeURIComponent',
    `${fnSrc}\nreturn clearSubgroupCooldown;`)(api, async () => { refreshed++; }, (msg, kind) => toasts.push([msg, kind]), snapshot, encodeURIComponent);
  await fn('bg');
  return {calls, toasts, refreshed};
}
const snapshot = {endpoints: [
  {id:'b1', name:'B1', in_pool:true, pool_groups:['bg'], in_cooldown:true},
  {id:'b2', name:'B2', in_pool:true, pool_groups:['bg'], manual_unlock_required:true},
  {id:'b3', name:'B3', in_pool:true, pool_groups:['bg'], in_cooldown:false},
  {id:'x1', name:'X1', in_pool:true, pool_groups:['main'], in_cooldown:true},
]};
(async () => {
  const good = await run(snapshot, () => ({ok: true}));
  assert.deepEqual(good.calls, ['POST /api/endpoints/b1/clear-error', 'POST /api/endpoints/b2/clear-error'], 'only frozen members are cleared, via the endpoint route');
  assert.equal(good.refreshed, 1, 'refreshes once after the batch');
  assert.deepEqual(good.toasts, [['已解除整组冻结（2 个端点）', 'success']], 'success toast reports the count');

  const partial = await run(snapshot, url => ({ok: !url.includes('/b2/')}));
  assert.equal(partial.toasts.length, 1);
  assert.equal(partial.toasts[0][1], 'error', 'partial failure is reported as an error');
  assert.match(partial.toasts[0][0], /已解除 1 个端点，1 个失败：B2/, 'partial failure names the endpoint');

  const none = await run({endpoints: [{id:'b3', name:'B3', in_pool:true, pool_groups:['bg']}]}, () => ({ok: true}));
  assert.deepEqual(none.calls, [], 'no request when nothing is frozen');
  assert.equal(none.toasts[0][1], 'error', 'empty state is reported instead of a fake success');

  console.log('subgroup unfreeze UI ok');
})();
