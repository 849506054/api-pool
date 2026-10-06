// 组内选择性轮换（2026-10-06）前端自检：node test_group_rotate_members.js
// 口径：组弹窗「端点轮换」配置项内，成员选择默认折叠；点摘要才查询组内成员并列出勾选；不勾 = 全体成员参与。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 1) 控件与轮换次数同行；成员选择默认折叠、点击才查询
assert(/<div style="display:flex;gap:8px;align-items:flex-start">\s*\n\s*<input type="number" id="gRotate"[^>]*>\s*\n\s*<details class="gm-dd" id="gRotateMembersBox" ontoggle="gmLoadMembers\(this\)">/.test(src), '轮换次数与成员折叠选择器同行');
assert(/<summary id="gRotateMembersSum" title="点击查询组内成员并勾选；不勾选 = 全部成员参与">\$\{gmSummary\}<\/summary>/.test(src), '摘要即查询入口');
assert(/<div class="gm-list" id="gRotateMembers" onchange="gmUpdateSummary\(\)"><\/div>/.test(src), '列表默认不预渲染（点击查询后填充）');
assert(!/<details[^>]*\sopen/.test(src), '默认折叠，不自动展开');
// 2) 查询动作：拉最新数据后按本组直接成员（in_pool + pool_groups 含本组）列出，按组内优先级排序
assert(/async function gmLoadMembers\(box\)\{/.test(src), '查询函数存在');
assert(/await refresh\(\); \/\/ 查询：拉最新 \/api\/endpoints \+ \/api\/groups/.test(src), '查询走 refresh() 拉最新');
assert(/const eps=\(poolSnapshot\.endpoints\|\|\[\]\)\.filter\(e=>e\.in_pool&&gmGroupsOf\(e\)\.includes\(gname\)\)/.test(src), '成员集=本组直接成员');
assert(/\.sort\(\(a,b\)=>gmPrioOf\(a,gname\)-gmPrioOf\(b,gname\)\);/.test(src), '按组内优先级排序');
assert(/function gmGroupsOf\(e\)\{return \(e\.pool_groups&&e\.pool_groups\.length\)\?e\.pool_groups:\['main'\];\}/.test(src), '组归属沿用 pool_groups 缺省 main 口径');
assert(/function gmPrioOf\(e,g\)\{return \(e\.priority_by_group&&e\.priority_by_group\[g\]!==undefined\)\?e\.priority_by_group\[g\]:e\.priority;\}/.test(src), '组内优先级沿用 priority_by_group 口径');
assert(/if\(!gname\)\{list\.innerHTML='<div class="gm-empty">先填分组名称并保存，再查询成员<\/div>'/.test(src), '新建组未命名时不查询');
assert(/box\.dataset\.loaded='1';/.test(src), '查询结果只取一次（不覆盖未保存的勾选）');
// 3) 回填：查询结果按组内已存 rotate_members 预勾选
assert(/const sel=new Set\(Array\.isArray\(g\.rotate_members\)\?g\.rotate_members:\[\]\);/.test(src), '按已存 rotate_members 预勾选');
assert(/<input type="checkbox" value="\$\{esc\(e\.id\)\}" \$\{sel\.has\(e\.id\)\?'checked':''\}>/.test(src), '复选框按 id 预勾选');
// 4) 空成员兜底 + label 包裹（点击整行即勾选，无需自定义浮层）
assert(/'<div class="gm-empty">组内暂无成员<\/div>'/.test(src), '无成员时给出占位提示');
assert(/<label class="gm-row">/.test(src), '复选框行用原生 label 包裹');
// 5) 保存：展开过才按勾选取值，否则沿用已存值（避免清空）
assert(/const rotate_members=\(rmBox&&rmBox\.dataset\.loaded==='1'&&rmEl\)/.test(src), '保存收集勾选的端点 id');
assert(/\.rotate_members:null\)\|\|\[\]\);/.test(src), '未展开查询时沿用已存选择');
assert(/log_level,rotate_requests,rotate_members\}\)/.test(src), 'PUT/POST 携带 rotate_members');
// 6) 摘要随勾选刷新
assert(/function gmUpdateSummary\(\)\{/.test(src), '摘要刷新函数存在');
assert(/参与轮换的成员：\$\{sel\?\`已选 \$\{sel\} 个\`:'全部'\}/.test(src), '摘要显示已选个数/全部');
// 7) 样式：复选框显式小尺寸（不继承 .form-group input 的框体），行用 flex，不引入自定义浮层
assert(/\.gm-row input\[type=checkbox\]\{flex:none;width:13px;height:13px;min-width:0;padding:0;margin:0;border:none/.test(src), '复选框显式尺寸，不继承表单输入框样式');
assert(/\.gm-row,\.form-group \.gm-row\{display:flex;align-items:center;gap:5px;padding:2px 6px;margin-bottom:0;font-size:11px;font-weight:400;/.test(src), '成员行用 flex 且覆盖 .form-group label 样式');
assert(/\.gm-dd\{position:relative;flex:0 0 auto;min-width:0\}/.test(src), '按钮=内容宽，不拉伸');
assert(/<input type="number" id="gRotate"[^>]*style="flex:1 1 auto;min-width:0"/.test(src), '同行次数输入占满余宽（行内不留空档）');
assert(!/\.gm-dd\[open\]\{flex/.test(src), '展开面板与按钮同宽（同一元素，不另设宽度）');
assert(/\.gm-list\{position:absolute;left:0;right:0;top:calc\(100% \+ 4px\);max-height:132px/.test(src), '展开面板绝对定位且宽度=按钮宽度');
assert(!/gm-popover|gm-dropdown|createPortal/.test(src), '不使用自定义浮层');
// 8) 选中成员在池卡打标
assert(/const rotating=\(gRotSel\.rotate_requests>0\)&&\(gRotSel\.rotate_members\|\|\[\]\)\.includes\(ep\.id\);/.test(src), '池卡按本组 rotate_members 判定');
assert(/🔄 轮换<\/span>/.test(src), '选中成员卡片带轮换徽标');
console.log('OK: 组内选择性轮换前端自检通过');
