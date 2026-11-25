/* Offline checks for naming started tasks: node scripts/test_started_tasks.cjs.
 * Uses the client's installed TypeScript compiler; no client, agent or game.
 */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../client/node_modules/typescript');

function load(path, stubs = {}) {
  const source = readFileSync(resolve(__dirname, '..', 'client/src', path), 'utf8');
  const context = { exports: {}, require: (id) => stubs[id] };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: path });
  return context.exports;
}

const { pairStartedTasks } = load('utils/startedTasks.ts');
const launch = { name: 'launch', selectedTask: { id: 'sel-launch' } };
const mailbox = { name: 'mailbox', selectedTask: { id: 'sel-mailbox' } };
const names = (pairs) => pairs.map(([id, item]) => [id, item.name]);

test('windows pairs returned IDs with submitted tasks by position', () => {
  assert.deepEqual(names(pairStartedTasks([7, 8], [launch, mailbox], { byBackendMapping: false })),
    [[7, 'launch'], [8, 'mailbox']]);
  assert.deepEqual(names(pairStartedTasks([7, 8, 9], [launch], { byBackendMapping: false })), [[7, 'launch']]);
});

test('macos names the mailbox correctly when the launch task before it was refused', () => {
  // [MXU_LAUNCH, mailbox]: the backend skipped the launch task, so the only ID is the mailbox's.
  const pairs = pairStartedTasks([8], [launch, mailbox],
    { byBackendMapping: true, mappings: { 8: 'sel-mailbox' } });
  assert.deepEqual(names(pairs), [[8, 'mailbox']]);
});

test('macos registers no names rather than guessing by position without the mapping', () => {
  assert.equal(pairStartedTasks([8], [launch, mailbox], { byBackendMapping: true }).length, 0);
  assert.equal(pairStartedTasks([8], [launch, mailbox], { byBackendMapping: true, mappings: {} }).length, 0);
});

/** The real taskStopService with its imports stubbed; every Map it creates records its reads and writes. */
function loadStopService(controllers, selected) {
  const source = readFileSync(resolve(__dirname, '..', 'client/src/services/taskStopService.ts'), 'utf8');
  const sent = [];
  const stop = { release: null };
  const maaService = {
    stopTask: (id) => { sent.push(['stopTask', id]); return new Promise((done) => { stop.release = done; }); },
    isRunning: async () => false,
    stopAgent: async (id) => { sent.push(['stopAgent', id]); },
  };
  const store = {
    projectInterface: { agent: null, controller: controllers, resource: [] },
    instances: Object.keys(selected).map((id) => ({ id, name: id })), selectedController: selected, selectedResource: {},
    updateInstance() {}, setInstanceTaskStatus() {}, setInstanceCurrentTaskId() {}, clearScheduleExecution() {},
  };
  const helpers = load('stores/helpers.ts', { '@/utils/optionHelpers': { findSwitchCase() {} } });
  const maps = [];
  class RecordingMap extends Map {
    constructor(...args) { super(...args); this.accesses = []; maps.push(this); }
    get(key) { this.accesses.push(['get', key]); return super.get(key); }
    set(key, value) { this.accesses.push(['set', key]); return super.set(key, value); }
    has(key) { this.accesses.push(['has', key]); return super.has(key); }
  }
  const stubs = {
    '@/types/interface': { normalizeAgentConfigs: () => [] },
    '@/utils/logger': { loggers: { task: { info() {}, warn() {} } } },
    '@/utils/paths': { isTauri: () => true },
    '@/stores/appStore': { useAppStore: { getState: () => store } },
    '@/stores/helpers': helpers,
    './maaService': { maaService },
  };
  const context = { exports: {}, require: (id) => stubs[id], setTimeout, Map: RecordingMap, Promise };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: 'taskStopService.ts' });
  // Module order: stopPromises first, then the Android stop generations.
  return { service: context.exports, sent, stop, generations: maps[1] };
}

const CONTROLLERS = [{ name: 'win', type: 'Win32' }, { name: 'mac', type: 'MacOS' }, { name: 'nikki_android', type: 'Adb' }];

test('every stop request is counted at once, before the stop itself is sent', async () => {
  // An Android start compares this count before it submits and after the backend answered (Toolbar.tsx); only
  // Adb instances are counted (see the next test).
  const { service, sent, stop } = loadStopService(CONTROLLERS, { A: 'nikki_android', B: 'nikki_android' });
  const { stopGeneration, stopInstanceTasks } = service;
  assert.equal(stopGeneration('A'), 0);
  const first = stopInstanceTasks('A');
  assert.equal(stopGeneration('A'), 1);  // counted synchronously, while the stop request is still in flight
  const second = stopInstanceTasks('A');  // joins the running stop, still counted
  assert.equal(stopGeneration('A'), 2);
  assert.equal(stopGeneration('B'), 0);
  stop.release();
  assert.equal(await first, true);
  assert.equal(await second, true);
  assert.deepEqual(sent, [['stopTask', 'A']]);
});

test('Win32 and native Mac stops neither read nor write the Android stop generations', async () => {
  const { service, sent, stop, generations } = loadStopService(CONTROLLERS, { W: 'win', M: 'mac' });
  for (const id of ['W', 'M']) {
    const stopping = service.stopInstanceTasks(id);
    stop.release();
    assert.equal(await stopping, true);
  }
  assert.deepEqual(sent, [['stopTask', 'W'], ['stopTask', 'M']]);  // the stop itself is sent as before
  assert.deepEqual(generations.accesses, []);
  assert.equal(generations.size, 0);
});

test('Win32 and native Mac starts in the toolbar do not read the Android stop generations', () => {
  const toolbar = readFileSync(resolve(__dirname, '..', 'client/src/components/Toolbar.tsx'), 'utf8');
  const calls = toolbar.split('\n').filter((line) => line.includes('stopGeneration(') && !line.includes('import '));
  assert.ok(calls.length >= 2);
  for (const line of calls) assert.match(line, /androidStart (\?|&&) .*stopGeneration\(/, line.trim());
});
