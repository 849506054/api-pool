// 日志卡按钮布局自检：node test_log_card_buttons.js
// 口径：两份日志卡（实时日志 / 对话日志）功能按钮——清空统一命名且居右，暂停紧跟卡片标题居左，实时日志不再有复制按钮。
const fs=require('fs'),path=require('path');
const candidates=[path.join(__dirname,'..','static','index.html'),path.join(__dirname,'..','index.html')];
const src=fs.readFileSync(candidates.find(p=>fs.existsSync(p)),'utf8');
function assert(cond,msg){ if(!cond){ console.error('FAIL: '+msg); process.exit(1);} }

// 清空统一命名；复制按钮与其函数一并下线（不留死代码）
assert(!/清空记录/.test(src), '不再出现「清空记录」');
assert(!/copySysLogs/.test(src), '不再出现 copySysLogs');
assert(!/📋 复制/.test(src), '不再出现复制按钮');
assert(!/Audit Logs/.test(src), '对话日志标题不含 Audit Logs');

// 两个清空按钮均居右（flex 容器内 margin-left:auto）
assert(/onclick="clearSysLogs\(\)" style="color:var\(--red\); margin-left:auto; padding:2px 8px;">🗑 清空</.test(src), '实时日志清空居右');
assert(/onclick="clearChatLogs\(\)" style="color:var\(--red\); margin-left:auto; padding:2px 8px;">🗑 清空</.test(src), '对话日志清空居右');

// 暂停按钮紧跟卡片标题之后，且不再依赖 float
for (const [title,pause,clear] of [
  ['<span class="icon">📝</span> 实时日志', 'id="logPauseBtn"', 'onclick="clearSysLogs()"'],
  ['<span class="icon">💬</span> 对话日志', 'id="clPauseBtn"', 'onclick="clearChatLogs()"'],
]) {
  const iT=src.indexOf(title), iP=src.indexOf(pause), iC=src.indexOf(clear);
  assert(iT>=0 && iP>iT && iC>iP, `按钮顺序（标题→暂停→清空）: ${pause}`);
  assert(!/float:right/.test(src.slice(src.indexOf(title), iC)), `暂停/清空不再用 float:right: ${pause}`);
}
console.log('OK: 日志卡按钮布局自检通过');
