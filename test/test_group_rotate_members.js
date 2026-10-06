// 组内选择性轮换（2026-10-06）前端自检：node test_group_rotate_members.js
// 口径：组弹窗「端点轮换」配置项内列出本组直接成员（复选框列表），勾选者参与轮换；不勾 = 全体成员参与。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 1) 控件落在轮换配置项同一 form-group 内，且紧跟次数输入
assert(/<input type="number" id="gRotate"[^>]*>\s*\n\s*<div style="font-size:11px;color:var\(--text-dim\);margin:6px 0 4px">参与轮换的成员（不选 = 全部成员参与）<\/div>\s*\n\s*<div class="gm-list" id="gRotateMembers">\$\{gmList\}<\/div>/.test(src), '轮换配置项内含成员复选框列表');
// 2) 成员集来源：本组直接成员（in_pool + pool_groups 含本组），按组内优先级排序
assert(/const gmEps=\(poolSnapshot\.endpoints\|\|\[\]\)\.filter\(e=>e\.in_pool&&gmGroups\(e\)\.includes\(g\.name\)\)\.sort\(\(a,b\)=>gmPrio\(a\)-gmPrio\(b\)\);/.test(src), '成员集=本组直接成员并按组内优先级排序');
assert(/const gmGroups=\(e\)=>\(\(e\.pool_groups&&e\.pool_groups\.length\)\?e\.pool_groups:\['main'\]\);/.test(src), '组归属沿用 pool_groups 缺省 main 口径');
assert(/const gmPrio=\(e\)=>\(\(e\.priority_by_group&&e\.priority_by_group\[g\.name\]!==undefined\)\?e\.priority_by_group\[g\.name\]:e\.priority\);/.test(src), '组内优先级沿用 priority_by_group 口径');
// 3) 回填：编辑时按 g.rotate_members 预勾选
assert(/const gmSel=new Set\(Array\.isArray\(g\.rotate_members\)\?g\.rotate_members:\[\]\);/.test(src), '编辑回填 rotate_members');
assert(/<input type="checkbox" value="\$\{esc\(e\.id\)\}" \$\{gmSel\.has\(e\.id\)\?'checked':''\}>/.test(src), '复选框按 id 预勾选');
// 4) 空成员兜底 + label 包裹（点击整行即勾选，无需自定义浮层）
assert(/'<div class="gm-empty">组内暂无成员<\/div>'/.test(src), '无成员时给出占位提示');
assert(/<label class="gm-row">/.test(src), '复选框行用原生 label 包裹');
// 5) 保存收集 + PUT/POST 携带
assert(/const rotate_members=rmEl\?Array\.from\(rmEl\.querySelectorAll\('input\[type=checkbox\]:checked'\)\)\.map\(c=>c\.value\):\[\];/.test(src), '保存收集勾选的端点 id');
assert(/log_level,rotate_requests,rotate_members\}\)/.test(src), 'PUT/POST 携带 rotate_members');
// 6) 样式复用模型选择器同构写法，不引入自定义浮层
assert(/\.gm-row\{display:grid;grid-template-columns:20px 1fr;/.test(src), '成员行与 .mb-row 同构的网格行');
assert(!/gm-popover|gm-dropdown|createPortal/.test(src), '不使用自定义浮层');
console.log('OK: 组内选择性轮换前端自检通过');
