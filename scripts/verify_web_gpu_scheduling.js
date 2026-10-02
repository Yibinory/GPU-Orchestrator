'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

async function run() {
  const nodes = new Map();
  const handlers = [];
  const document = {
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, {innerHTML: '', textContent: '', classList: {toggle() {}, add() {}, remove() {}, contains() {return false;}}});
      return nodes.get(selector);
    },
    querySelectorAll() {return [];},
    addEventListener(type, handler) {handlers.push(handler);}
  };
  const context = vm.createContext({document, console, setTimeout, clearTimeout, Set});
  const source = fs.readFileSync(path.join(__dirname, '..', 'static', 'app.js'), 'utf8');
  vm.runInContext(source.slice(0, source.indexOf('/* ---------- events ---------- */')) + '\nglobalThis.testUI = UI;', context);
  const gpu = {index: 0, uuid: 'GPU-test', name: 'Demo GPU', memory_total_mb: 24576, memory_used_mb: 512, scheduler_idle: true};
  context.testUI.data = {active_server_id: 'second', server_states: {second: {snapshot: {gpus: [gpu]}, conda: {envs: []}}}};
  for (const blocked of [false, true]) {
    gpu.scheduling_blocked = blocked;
    context.renderGpuFleet();
    context.renderBenchmarks();
    for (const selector of ['#gpu-grid', '#benchmark-grid']) {
      const html = document.querySelector(selector).innerHTML;
      assert.match(html, /data-server-id="second"/);
      assert.match(html, /data-gpu-uuid="GPU-test"/);
      assert.ok(html.includes('aria-pressed="' + blocked + '"'));
      assert.ok(html.includes(blocked ? '屏蔽' : '正常'));
      assert.ok(html.includes('data-benchmark="0"'), 'Benchmark action was lost');
    }
    if (blocked) assert.ok(!document.querySelector('#gpu-grid').innerHTML.includes('空闲可调度'));
  }
  let calls = [], release, rendered = 0, toast = '';
  context.postJson = (url, body) => {
    calls.push({url, body});
    return new Promise(resolve => {release = resolve;});
  };
  context.renderAll = () => {rendered++;};
  context.showToast = message => {toast = message;};
  const label = {textContent: '正常'};
  const button = {dataset: {serverId: 'second', gpuScheduling: '0', gpuUuid: 'GPU-test', blocked: 'false'}, disabled: false, classList: {add() {}}, setAttribute() {}, querySelector: () => label};
  const pending = context.toggleGpuScheduling(button);
  assert.equal(button.disabled, true);
  assert.equal(label.textContent, '保存中…');
  assert.equal(calls[0].url, '/api/gpus/scheduling');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), {server_id: 'second', gpu_index: 0, gpu_uuid: 'GPU-test', blocked: true});
  assert.match(context.gpuSchedulingControl(gpu, 'second'), / disabled/);
  await context.toggleGpuScheduling(button);
  assert.equal(calls.length, 1, 'Repeated click submitted twice');
  release(context.testUI.data);
  await pending;
  assert.equal(context.testUI.gpuSchedulingPending.size, 0);
  assert.equal(rendered, 1);
  assert.match(toast, /已屏蔽/);
  button.dataset.blocked = 'true';
  const restoring = context.toggleGpuScheduling(button);
  assert.equal(calls[1].body.blocked, false);
  release(context.testUI.data);
  await restoring;
  assert.match(toast, /恢复正常/);
  context.postJson = async () => {throw new Error('Synthetic save failure');};
  await context.toggleGpuScheduling(button);
  assert.equal(context.testUI.gpuSchedulingPending.size, 0);
  assert.equal(toast, 'Synthetic save failure');
  assert.equal(handlers.length, 1, 'Toggle event handler missing');
  let delegated = 0;
  context.toggleGpuScheduling = target => {assert.equal(target, button); delegated++;};
  handlers[0]({target: {closest: () => button}});
  assert.equal(delegated, 1, 'Click did not reach the scheduling action');
  console.log('PASS: web resource/benchmark cards, status markup, server/UUID targeting, both toggle payloads, repeated-click guard and error recovery.');
}
run().catch(error => {console.error(error); process.exitCode = 1;});
