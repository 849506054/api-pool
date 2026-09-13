#!/usr/bin/env node
// node test/test_group_context_input.js [index.html]
// 组级上下文长度输入解析（2026-09-13）：纯数字=tokens，支持 K/M 后缀。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {Script} = require('node:vm');

const html = fs.readFileSync(process.argv[2] || path.join(__dirname, '../static/index.html'), 'utf8');
const match = html.match(/function parseCtxTokens\(raw\)\{[\s\S]*?\n\}/);
assert(match, 'parseCtxTokens 未在 index.html 中找到');
const sandbox = {};
new Script(match[0] + '\nthis.parseCtxTokens = parseCtxTokens;').runInNewContext(sandbox);
const parse = sandbox.parseCtxTokens;

assert.equal(parse('1048576'), 1048576, '纯数字按 tokens');
assert.equal(parse(' 202752 '), 202752, '首尾空白容忍');
assert.equal(parse('1,048,576'), 1048576, '千分位分隔符容忍');
assert.equal(parse('1M'), 1000000, 'M 后缀');
assert.equal(parse('1m'), 1000000, '小写后缀');
assert.equal(parse('1.5M'), 1500000, '小数后缀');
assert.equal(parse('512K'), 512000, 'K 后缀');
assert.equal(parse('200k'), 200000, '小写 k');
assert.equal(parse('0'), 0, '0 = 不声明');
assert.equal(parse(''), 0, '空 = 不声明');
assert.equal(parse(null), 0, 'null = 不声明');
assert.ok(Number.isNaN(parse('abc')), '非法文本应 NaN');
assert.ok(Number.isNaN(parse('12X')), '未知后缀应 NaN');
assert.ok(Number.isNaN(parse('1M2')), '多余字符应 NaN');
// 边界：越界值由 savePoolGroup 拒绝（2000–10,000,000），解析层不夹取
assert.equal(parse('1K'), 1000, '解析层保留原值，越界判定交给调用方');

console.log('PASS: parseCtxTokens');
