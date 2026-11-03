// node test/test_group_membership_ui.js [index.html]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Script} = require('node:vm');
const html = fs.readFileSync(process.argv[2] || path.join(__dirname, '../static/index.html'), 'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new Script(m[1]);
const css = html.match(/<style\b[^>]*>([\s\S]*?)<\/style>/)[1];
assert(!/^\s*\/\//m.test(css), 'style blocks use CSS comment syntax');
assert(css.replace(/\/\*[\s\S]*?\*\//g, '').includes('.ep-item.in-pool.subgroup-entry{border-left:3px solid var(--accent)}'), 'subgroup border rule is intact');
function section(start, end) {
  const a = html.indexOf(start), b = html.indexOf(end, a);
  assert(a >= 0 && b > a, `source boundaries: ${start}`);
  return html.slice(a, b);
}
const source = html.match(/function epErr\(ep\)\{[^\n]*\}/)[0] + '\n'
  + section("let poolGroupFilter='main';", '\nasync function endpointModels(')
  + section('function openGroupModal(', '\nasync function deletePoolGroup(')
  + section("let chainGroupFilter='main';", '\nasync function runHealthCheck(');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const decode = value => value.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const nodes = Object.fromEntries(['poolList','poolCount','chainList','groupSummary'].map(id => [id, {innerHTML:'', textContent:''}]));
let modalIds = [];
function parseControls(markup) {
  for (const m of markup.matchAll(/<(input|select|button|details|div)\b([^>]*\bid="([^"]+)"[^>]*)>([\s\S]*?)(?=<\/\1>|$)/g)) {
    const [, tag, attrs, id, inner] = m;
    const options = tag === 'select' ? [...inner.matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map(o => ({value:decode(o[1].match(/value="([^"]*)"/)?.[1] || ''), disabled:/\bdisabled\b/.test(o[1]), selected:/\bselected\b/.test(o[1]), text:decode(o[2])})) : [];
    nodes[id] = {value:decode(attrs.match(/value="([^"]*)"/)?.[1] || ''), disabled:/\bdisabled\b/.test(attrs), dataset:{}, innerHTML:inner, options, textContent:'', querySelectorAll:()=>[]};
    if (tag === 'select') nodes[id].value = (options.find(o=>o.selected) || options[0] || {}).value || '';
    modalIds.push(id);
  }
  // Inputs are void elements; parse them independently of containing fields.
  for (const m of markup.matchAll(/<input\b([^>]*\bid="([^"]+)"[^>]*)>/g)) {
    nodes[m[2]] = {value:decode(m[1].match(/value="([^"]*)"/)?.[1] || ''), disabled:/\bdisabled\b/.test(m[1])};
    modalIds.push(m[2]);
  }
  // Nested form fields may contain selects skipped by the generic element pass.
  for (const m of markup.matchAll(/<select\b([^>]*\bid="([^"]+)"[^>]*)>([\s\S]*?)<\/select>/g)) {
    const options = [...m[3].matchAll(/<option\b([^>]*)>([\s\S]*?)<\/option>/g)].map(o => ({value:decode(o[1].match(/value="([^"]*)"/)?.[1] || ''), disabled:/\bdisabled\b/.test(o[1]), selected:/\bselected\b/.test(o[1]), text:decode(o[2])}));
    nodes[m[2]] = {options, disabled:/\bdisabled\b/.test(m[1]), multiple:/\bmultiple\b/.test(m[1]), value:(options.find(o=>o.selected)||options[0]||{}).value||''};
    modalIds.push(m[2]);
  }
}
const document = {
  getElementById:id=>nodes[id] || null,
  createElement:()=>({innerHTML:'', remove(){for (const id of modalIds) delete nodes[id];modalIds=[];delete nodes.groupEditOverlay;}}),
  body:{appendChild(o){nodes[o.id]=o;parseControls(o.innerHTML);}}
};
const window = {};
const calls = [], notices = [];
let refreshes = 0, nextFailure = '', data;
async function api(method, url, body) {
  calls.push({method,url,body});
  if (nextFailure && url.endsWith(nextFailure)) {nextFailure='';return {ok:false,error:'fixture failure'};}
  const name=decodeURIComponent(url.split('/')[3] || '');
  const g=data.groupDefs.find(g=>g.name===name);
  if (method==='PUT') {Object.assign(g,body);return {ok:true,name:g.name};}
  if (url.endsWith('/join-group')) g.priority_by_group[body.group] = body.priority || 2;
  if (url.endsWith('/leave-group')) delete g.priority_by_group[body.group];
  return {ok:true};
}
const ui = new Function('document','window','esc','escAttr','fmtTime','fmtCdSec','prefetchPoolModels','api','toast','confirm','refresh','refreshLogViews', `
  let poolSnapshot;
  ${source}
  return {openGroupModal, savePoolGroup, closeGroupModal, setPoolGroupFilter, setChainGroupFilter,
    load(d){poolSnapshot=d;window._groupDefs=d.groupDefs;window._poolGroups=d.groups;renderPoolList(d.endpoints);renderChain(d.chain);},
    run(code, el={}){return eval('(async function(){'+code+'}).call(el)');}};
`)(document,window,esc,esc,s=>`${s}s`,s=>`${s}s`,()=>{},api,(...args)=>notices.push(args),()=>true,async()=>{refreshes++;ui.load(data);},()=>{});
const ep=(name, groups, priority_by_group)=>({id:name,name,model:'model',enabled:true,in_pool:true,is_vision:false,priority:1,priority_by_group,pool_groups:groups,current_groups:[]});
function reset() {
  ui.closeGroupModal();calls.length=0;notices.length=0;refreshes=0;nextFailure='';
  data={groupDefs:[{name:'main',type:'mixed',is_main:true},{name:'vision',type:'mixed',is_vision:true},{name:'work',type:'mixed'},
    {name:'free',type:'mixed'},{name:'child',type:'dedicated',model:'model',main_priority:99,priority_by_group:{main:2,work:1}},
    {name:'other-child',type:'dedicated',priority_by_group:{free:1}}],
    groups:{main:{current_subgroup:'child'},work:{current_subgroup:null},child:{current_endpoint:'Child endpoint'},free:{},vision:{}},
    endpoints:[ep('Main native',['main'],{main:1}),ep('Work native',['work'],{work:2}),ep('Child endpoint',['child'],{child:1}),ep('Other endpoint',['other-child'],{'other-child':1})],chain:[]};
  data.chain=[...data.endpoints,
    {...ep('CHILD',['main'],{main:2}),id:'opaque:main:001',subgroup:'child',is_subgroup_entry:true,is_current:true,current_groups:['main']},
    {...ep('CHILD',['work'],{work:1}),id:'opaque:work:002',subgroup:'child',is_subgroup_entry:true,is_current:false}];
  ui.load(data);ui.setPoolGroupFilter('main');
}
function joinSelect() {return nodes.gJoinGroup;}
function card(name='child') {
  return nodes.poolList.innerHTML.split(/(?=<div class="ep-item)/).find(s=>s.includes(`>${esc(name)}</span>`));
}
function action(markup, tag, title) {
  const match=[...markup.matchAll(new RegExp(`<${tag}\\b[^>]*>`, 'g'))].find(m=>m[0].includes(title));
  assert(match, `action ${title}`);
  return decode(match[0].match(/on(?:click|change)="([^"]+)"/)?.[1] || '');
}
function rankValues(markup) {return [...markup.matchAll(/<option value="(\d+)"/g)].map(m=>+m[1]);}
function badges() {return [...nodes.poolList.innerHTML.matchAll(/badge-priority">#(\d+)<\/span>/g)].map(m=>+m[1]);}
(async()=>{
  reset();
  assert(nodes.poolList.innerHTML.includes('>🎯 child 1</button>'), 'dedicated group has target icon');
  assert(nodes.poolList.innerHTML.includes('>🎯 other-child 1</button>'), 'every dedicated group is marked');
  assert(nodes.poolList.innerHTML.includes('>🏊main 2</button>'));
  assert(nodes.poolList.innerHTML.includes('>👁️vision 0</button>'));
  assert(nodes.poolList.innerHTML.includes('>work 2</button>'), 'mixed group keeps plain name');
  assert(nodes.poolList.innerHTML.includes('专用组 · 绑定模型：model'));
  assert(!nodes.chainList.innerHTML.includes('🎯'), 'icon is scoped to pool-management buttons');
  const iconGroup=data.groupDefs.find(g=>g.name==='child');
  iconGroup.type='mixed';ui.load(data);
  assert(!nodes.poolList.innerHTML.includes('>🎯 child'), 'type controls icon rather than name');
  iconGroup.type='dedicated';iconGroup.name='renamed-target';ui.load(data);
  assert(nodes.poolList.innerHTML.includes('>🎯 renamed-target 0</button>'), 'renamed empty dedicated group stays marked');
  reset();ui.openGroupModal('child');
  const select=joinSelect();
  assert(select, 'dedicated edit renders a native target select');
  assert.equal(select.multiple,false);assert.equal(select.value,'');assert.equal(select.disabled,false);
  assert.deepEqual(select.options.map(o=>o.value),['','main','work','free']);
  for (const name of ['main','work']) assert.deepEqual(select.options.find(o=>o.value===name),{value:name,disabled:true,selected:false,text:`${name}（已在组内）`});
  assert.equal(select.options.find(o=>o.value==='free').disabled,false);
  joinSelect().value='main';await ui.savePoolGroup('child');
  assert.equal(calls.length,1,'an already joined target remains a no-op');calls.length=0;ui.openGroupModal('child');
  await ui.savePoolGroup('child');
  assert.deepEqual(calls.map(c=>c.method),['PUT'],'blank selection only saves fields');
  for (const name of ['main','vision','work']) {ui.openGroupModal(name);assert.equal(joinSelect(),undefined,`${name} has its own form fields`);ui.closeGroupModal();}
  ui.openGroupModal(null);assert.equal(joinSelect(),undefined);ui.closeGroupModal();

  reset();ui.openGroupModal('child');joinSelect().value='free';nodes.gName.value='renamed';
  await ui.savePoolGroup('child');
  assert.deepEqual(calls.map(c=>[c.method,c.url]),[['PUT','/api/groups/child'],['POST','/api/groups/renamed/join-group']]);
  assert.deepEqual(calls[1].body,{group:'free'},'append leaves rank assignment to the backend');
  assert(refreshes>0);ui.openGroupModal('renamed');assert.equal(joinSelect().disabled,true);assert.equal(joinSelect().value,'');
  assert(joinSelect().options.slice(1).every(o=>o.disabled));
  joinSelect().value='main';calls.length=0;await ui.savePoolGroup('renamed');assert.equal(calls.length,1,'all-joined control is a no-op');

  reset();ui.openGroupModal('child');joinSelect().value='free';nextFailure='/api/groups/child';
  await ui.savePoolGroup('child');assert.equal(calls.length,1,'failed save keeps membership untouched');ui.closeGroupModal();
  reset();ui.openGroupModal('child');joinSelect().value='free';nextFailure='/join-group';
  await ui.savePoolGroup('child');assert(notices.some(([s,t])=>t==='error'&&s.includes('fixture failure')),'join error reaches the user');
  assert(!notices.some(([s,t])=>t==='success'),'partial failure is not reported as full success');

  reset();const snapshot=JSON.stringify(data);assert.deepEqual(badges(),[1,2]);
  assert(card().includes('subgroup-entry current'));assert.deepEqual(rankValues(card()),[1,2]);
  assert.equal(nodes.poolCount.textContent,'2 个成员');
  assert(nodes.poolList.innerHTML.indexOf('>Main native</span>')<nodes.poolList.innerHTML.indexOf('>child</span>'));
  assert(nodes.chainList.innerHTML.includes('subgroup-entry active'));
  assert(nodes.poolList.innerHTML.includes('🏊main 2</button>'));assert(nodes.poolList.innerHTML.includes('>work 2</button>'));
  ui.setPoolGroupFilter('work');assert.deepEqual(badges(),[1,2]);assert(card().includes('subgroup-entry"'));
  assert(!card().includes('badge-current'));assert.deepEqual(rankValues(card()),[1,2]);
  assert(nodes.poolList.innerHTML.indexOf('>child</span>')<nodes.poolList.innerHTML.indexOf('>Work native</span>'));
  assert(!nodes.chainList.innerHTML.includes('subgroup-entry active'),'chain current mark belongs to the viewed parent');
  const native=nodes.poolList.innerHTML.split(/(?=<div class="ep-item)/).find(s=>s.includes('>Work native</span>'));
  assert.deepEqual(rankValues(native),[1,2]);
  assert.equal(JSON.stringify(data),snapshot,'rendering preserves API snapshots');
  const subgroup=card();
  const rank=[...subgroup.matchAll(/<select\b[^>]*>/g)][0][0];
  await ui.run(decode(rank.match(/onchange="([^"]+)"/)[1]),{value:'2',blur(){}});
  await ui.run(action(subgroup,'button','手动切'));
  await ui.run(action(subgroup,'button','移出'));
  assert.deepEqual(calls.map(c=>[c.url,c.body]),[
    ['/api/groups/child/join-group',{group:'work',priority:2}],['/api/groups/child/switch-group',{group:'work'}],['/api/groups/child/leave-group',{group:'work'}]
  ]);
  assert.equal(data.groupDefs.find(g=>g.name==='child').priority_by_group.main,2);
  ui.openGroupModal('child');assert.equal(joinSelect().options.find(o=>o.value==='work').disabled,false,'leaving restores the target option');ui.closeGroupModal();
  await ui.run(action(subgroup,'button','编辑该子组'));assert.equal(nodes.poolCount.textContent,'1 个成员');assert(!nodes.poolList.innerHTML.includes('subgroup-entry'));
  assert(!nodes.chainList.innerHTML.includes('subgroup-entry'),'child filter shows its native members');
  ui.setChainGroupFilter('work');assert(nodes.chainList.innerHTML.includes('CHILD'));
  assert(!nodes.chainList.innerHTML.includes('作为 main 成员'),'chain description follows the viewed parent');
  ui.setPoolGroupFilter('vision');assert(!nodes.poolList.innerHTML.includes('subgroup-entry'));

  reset();const child=data.groupDefs.find(g=>g.name==='child');child.priority_by_group={};ui.load(data);
  assert(!card(),'empty membership map is authoritative');delete child.priority_by_group;child.main_priority=2;ui.load(data);assert(card(),'absent map uses legacy main rank');
  child.priority_by_group={main:2};child.type='mixed';ui.load(data);assert(!card(),'ordinary groups remain native parents');
  reset();data.endpoints=[];data.chain=[];ui.load(data);assert(card(),'joined empty groups remain visible');
  reset();data.groups.main.current_endpoint_name='Resolved child endpoint';data.groups.child.current_endpoint_name='Shared child endpoint';
  data.groups.work.current_subgroup='child';data.chain.find(e=>e.id==='opaque:work:002').is_current=false;
  ui.load(data);assert(nodes.poolList.innerHTML.includes('● Resolved child endpoint'));assert(card().includes('当前端点：Shared child endpoint'));
  ui.setPoolGroupFilter('work');assert(card().includes('subgroup-entry current'));assert(nodes.chainList.innerHTML.includes('subgroup-entry active'));

  reset();const tricky="child'\"\\<x>",parent="work'\"\\<p>";
  data.groupDefs.find(g=>g.name==='child').name=tricky;data.groupDefs.find(g=>g.name==='work').name=parent;
  data.groupDefs.find(g=>g.name===tricky).priority_by_group={[parent]:1};ui.load(data);ui.setPoolGroupFilter(parent);
  ui.openGroupModal(tricky);assert.equal(nodes.gName.value,tricky);assert(joinSelect().options.some(o=>o.value===parent&&o.disabled));ui.closeGroupModal();
  await ui.run(action(card(tricky),'button','手动切'));
  assert.deepEqual(calls[0],{method:'POST',url:`/api/groups/${encodeURIComponent(tricky)}/switch-group`,body:{group:parent}},'inline action round-trips quoted names');
  console.log('PASS: membership single-select; disabled joined options; save then one append; scoped cards/counts/ranks/current/actions; leave/rejoin; chain isolation; legacy map; escaped names');
})().catch(error=>{console.error(error);process.exitCode=1;});
