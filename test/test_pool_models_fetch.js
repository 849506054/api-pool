#!/usr/bin/env node
// node test/test_pool_models_fetch.js [index.html]
// 池内模型目录拉取（2026-10-01）：
//   1) 同一端点在途请求复用 → 不重复发上游请求
//   2) 成功结果 5 分钟缓存
//   3) 失败结果 60 秒短缓存（此前失败端点每个刷新周期重试一次）
//   4) 预取并发上限 2（不再占满浏览器同源连接）
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
const source = section('const endpointModelsCache=new Map();', '\nlet loadingPoolModelSelect=null;')
  + '\n' + section('async function endpointModels(endpointId){', '\nasync function loadPoolModelOptions(');

// ── api 桩：记录调用，可配置延迟/响应/并发度 ──
const calls = [];
let delayMs = 20;
let responder = () => ({ok: true, models: ['b', 'a', 'a']});
let concurrent = 0, maxConcurrent = 0;
const api = (method, p) => {
  calls.push(p);
  concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
  return new Promise(resolve => setTimeout(() => {
    concurrent--;
    resolve(responder(p));
  }, delayMs));
};
const sandbox = new Function('api', `
  ${source}
  return {endpointModels, prefetchPoolModels, cache:endpointModelsCache,
    get inflightCount(){return endpointModelsInflight.size},
    reset(){endpointModelsCache.clear();endpointModelsInflight.clear();modelsPrefetchRunning=false;}};
`)(api);

const countOf = id => calls.filter(p => p.includes(`/${id}/models`)).length;
const ageFailure = id => { // 把失败条目的时间戳往前拨，模拟 60 秒已过
  const entry = sandbox.cache.get(id);
  if (entry) entry.at -= 61_000;
};

(async () => {
  // 1) 在途去重 + 去重排序
  sandbox.reset(); calls.length = 0;
  const [x, y] = await Promise.all([sandbox.endpointModels('e1'), sandbox.endpointModels('e1')]);
  assert.equal(countOf('e1'), 1, '在途请求必须复用，只发一次');
  assert.deepEqual(x, ['a', 'b']);
  assert.deepEqual(y, ['a', 'b']);
  assert.equal(sandbox.inflightCount, 0, '在途表必须清空');

  // 2) 成功后 5 分钟内不再请求
  await sandbox.endpointModels('e1');
  assert.equal(countOf('e1'), 1, '成功结果应命中缓存');

  // 3) 失败 60 秒短缓存：期间不重试
  sandbox.reset(); calls.length = 0;
  responder = () => ({ok: false, error: 'missing required parameter: model'});
  await assert.rejects(() => sandbox.endpointModels('e2'), /missing required parameter/);
  await assert.rejects(() => sandbox.endpointModels('e2'), /missing required parameter/);
  assert.equal(countOf('e2'), 1, '失败后 60 秒内不得重试');
  ageFailure('e2');
  await assert.rejects(() => sandbox.endpointModels('e2'), /missing required parameter/);
  assert.equal(countOf('e2'), 2, '失败缓存过期后允许重试');

  // 4) 预取并发上限 2，且进行中不重复起队列
  sandbox.reset(); calls.length = 0; maxConcurrent = 0; delayMs = 15;
  responder = () => ({ok: true, models: ['m']});
  const ids = ['p1', 'p2', 'p3', 'p4', 'p5', 'p6'];
  sandbox.prefetchPoolModels(ids);
  sandbox.prefetchPoolModels(ids); // 重复调用不得叠加第二个队列
  await new Promise(r => setTimeout(r, 200));
  assert.equal(sandbox.inflightCount, 0, '预取结束后在途表应为空');
  assert.equal(calls.length, 6, `应只预取一轮，实际 ${calls.length}`);
  assert(maxConcurrent <= 2, `预取并发必须 ≤2，实际 ${maxConcurrent}`);
  assert(ids.every(id => countOf(id) === 1), '每个端点各拉一次');

  // 5) 预取吞掉失败，不产生 unhandled rejection
  sandbox.reset(); calls.length = 0;
  responder = p => p.includes('/p2/') ? {ok: false, error: 'boom'} : {ok: true, models: ['m']};
  sandbox.prefetchPoolModels(['p1', 'p2', 'p3']);
  await new Promise(r => setTimeout(r, 150));
  assert.equal(calls.length, 3, '失败的端点不得阻塞其余预取');

  console.log('PASS: 在途去重 / 成功缓存 / 失败短缓存 / 预取并发≤2 / 预取容错');
})().catch(err => { console.error(err); process.exit(1); });
