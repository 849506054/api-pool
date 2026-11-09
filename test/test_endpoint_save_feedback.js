// node test/test_endpoint_save_feedback.js [index.html]
// 端点保存按钮：点击即刻反馈（保存中…/disabled）+ 在途重复点击去重（2026-11-09）。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {Script} = require('node:vm');

const file = process.argv[2] || path.join(__dirname, '../static/index.html');
const html = fs.readFileSync(file, 'utf8');

// 1. 内嵌脚本语法
for (const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new Script(m[1]);

// 2. 行为：同一时刻的第二次点击被丢弃，按钮当场进入「保存中…」
const start = html.includes('let _endpointSaveInFlight=false;')
  ? html.indexOf('let _endpointSaveInFlight=false;')
  : html.indexOf('async function saveEndpoint(){');
const end = html.indexOf('async function batchAddEndpoints(){', start);
assert(start >= 0 && end > start, 'saveEndpoint block found');
const source = html.slice(start, end);

const button = {textContent: '保存', disabled: false, dataset: {}};
const nodes = {
  editName: {value: ''},
  fName: {value: 'ep1'},
  singleAddBtn: button,
};
let apiCalls = 0;
let resolveApi;
const pending = new Promise(resolve => { resolveApi = resolve; });
const toasts = [];

const ctx = {
  document: {getElementById: id => nodes[id] || null},
  readFormDraft: () => ({name: 'ep1', model: 'mdl', base_url: 'http://127.0.0.1:1', api_key: '[REDACTED_API_KEY]'}),
  _origDraft: {},
  api: () => { apiCalls += 1; return pending; },
  toast: (msg, kind) => toasts.push([kind, msg]),
  closeModal: () => {},
  refresh: () => {},
  JSON,
  Object,
  console,
};
vm.createContext(ctx);
const saveEndpoint = vm.runInContext(`${source}\n;saveEndpoint`, ctx);

(async () => {
  const first = saveEndpoint();
  assert.equal(button.disabled, true, 'button disabled on click');
  assert.equal(button.textContent, '保存中…', 'button label switches immediately');

  saveEndpoint();
  await Promise.resolve();
  assert.equal(apiCalls, 1, 'repeat click does not hit the API');

  resolveApi({ok: true});
  await first;
  assert.equal(apiCalls, 1);
  assert.equal(button.disabled, false, 'button re-enabled after save');
  assert.equal(button.textContent, '保存', 'button label restored');
  assert.ok(toasts.some(([kind, msg]) => kind === 'success' && msg === '已添加'), 'success toast shown');

  // 失败后按钮同样复原，用户可立即重试
  apiCalls = 0;
  let resolveFail;
  const failing = new Promise(resolve => { resolveFail = resolve; });
  ctx.api = () => { apiCalls += 1; return failing; };
  const third = vm.runInContext('saveEndpoint()', ctx);
  assert.equal(button.disabled, true);
  resolveFail({ok: false, error: 'boom'});
  await third;
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '保存');
  // 失败后立刻重试不被去重拦截
  vm.runInContext('saveEndpoint()', ctx);
  assert.equal(apiCalls, 2, 'retry after failure is allowed');

  console.log('ok: endpoint save feedback + in-flight dedupe');
})().catch(err => { console.error(err); process.exit(1); });
