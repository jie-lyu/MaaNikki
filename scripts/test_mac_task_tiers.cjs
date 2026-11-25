/* Offline checks for the Mac task tier markings: node scripts/test_mac_task_tiers.cjs.
 * Uses the client's installed TypeScript compiler and the shipped manifest; no client, agent or game.
 */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../client/node_modules/typescript');

const ROOT = resolve(__dirname, '..');

function load(path) {
  const source = readFileSync(resolve(ROOT, 'client/src', path), 'utf8');
  const context = { exports: {}, setTimeout, clearTimeout };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: path });
  return context.exports;
}

const tiers = load('utils/macTaskTiers.ts');
const manifest = JSON.parse(readFileSync(resolve(ROOT, 'agent/host/macos/preview_tasks.json'), 'utf8'));
// What the backend returns on macOS (commands::utils::task_tiers_view).
const mac = tiers.normalizeMacTaskTiers({
  verified: manifest.entries, experimental: manifest.experimental, excluded: manifest.excluded, notes: manifest.notes,
});

test('the Windows contract: empty tiers mark nothing', () => {
  const windows = tiers.normalizeMacTaskTiers({ verified: [], experimental: [], excluded: {}, notes: {} });
  for (const source of [windows, null, undefined, tiers.normalizeMacTaskTiers(null)]) {
    for (const entry of ['MaaNikki_Mailbox', 'MaaNikki_Home', 'MaaNikki_StartGame', 'MXU_LAUNCH', 'MaaNikki_Fishing']) {
      assert.equal(tiers.macTaskMark(source, entry), null);
    }
  }
  assert.equal(tiers.macExcludedText(null), '');
});

test('a manifest error alone marks nothing and keeps the error', () => {
  const broken = tiers.normalizeMacTaskTiers({ verified: [], experimental: [], excluded: {}, notes: {}, error: '无法读取 Mac 任务清单（x）' });
  assert.equal(broken.error, '无法读取 Mac 任务清单（x）');
  assert.equal(tiers.macTaskMark(broken, 'MaaNikki_Home'), null);
});

test('macOS marks verified, experimental and excluded entries from the shipped manifest', () => {
  assert.equal(tiers.macTaskMark(mac, 'MaaNikki_Mailbox').tier, 'verified');
  assert.equal(tiers.macTaskMark(mac, 'MaaNikki_CollectStarlightCrystals').note, manifest.notes.MaaNikki_CollectStarlightCrystals);
  const bling = tiers.macTaskMark(mac, 'MaaNikki_MinigameBling');
  assert.equal(bling.tier, 'experimental');
  assert.equal(bling.note, '每次进场或付费重试消耗 100 噗灵，默认 267 轮');
  assert.equal(tiers.macTaskMark(mac, 'MaaNikki_Dig').note, manifest.notes.MaaNikki_Dig);
  assert.equal(tiers.macTaskMark(mac, 'MaaNikki_Mailbox').note, undefined);
  const start = tiers.macTaskMark(mac, 'MaaNikki_StartGame');
  assert.equal(start.tier, 'excluded');
  assert.equal(tiers.macExcludedText(start), `Mac 不可用：${manifest.excluded.MaaNikki_StartGame}`);
  // Entries in no list (MXU built-ins, client-only features) are refused too.
  assert.deepEqual(JSON.parse(JSON.stringify(tiers.macTaskMark(mac, 'MXU_LAUNCH'))), { tier: 'excluded', reason: tiers.MAC_UNSUPPORTED_REASON });
  assert.equal(tiers.macTaskMark(mac, ''), null);
  assert.equal(tiers.macTaskMark(mac, undefined), null);
});

test('malformed backend values are dropped, not trusted', () => {
  const odd = tiers.normalizeMacTaskTiers({ verified: ['A', 1, ''], experimental: 'B', excluded: { C: 'why', D: 2 }, notes: [] });
  assert.deepEqual(JSON.parse(JSON.stringify(odd)), { verified: ['A'], experimental: [], excluded: { C: 'why' }, notes: {} });
});

test('macOS starts wait for the tiers and stop on a manifest error; other systems never wait', () => {
  // Before the tier response arrives the store holds null: a run (manual or scheduled) must not start.
  assert.equal(tiers.macStartBlockedReason('macos', null), tiers.MAC_TIERS_PENDING);
  assert.equal(tiers.macStartBlockedReason('macos', undefined), tiers.MAC_TIERS_PENDING);
  const broken = tiers.normalizeMacTaskTiers({ error: '无法读取 Mac 任务清单（x）' });
  assert.equal(tiers.macStartBlockedReason('macos', broken), '无法读取 Mac 任务清单（x）');
  // Once the response is in, the run may start.
  assert.equal(tiers.macStartBlockedReason('macos', mac), '');
  for (const os of ['windows', 'linux', '']) {
    for (const source of [null, undefined, mac, broken]) assert.equal(tiers.macStartBlockedReason(os, source), '');
  }
});

test('an empty or malformed macOS tier response is a load error that refuses starts', () => {
  for (const raw of [null, [], {}, { verified: [], experimental: [], excluded: {}, notes: {} },
    { verified: 'x', experimental: [], excluded: {}, notes: {} }, { verified: ['A'], experimental: [], excluded: [], notes: {} },
    { verified: ['A'], experimental: [], excluded: {} },
    // valid containers with bad members or overlapping tiers
    { verified: ['A'], experimental: [], excluded: {}, notes: { A: 7 } },
    { verified: ['A', 1], experimental: [], excluded: {}, notes: {} },
    { verified: ['A', 'A'], experimental: [], excluded: {}, notes: {} },
    { verified: ['A'], experimental: [''], excluded: {}, notes: {} },
    { verified: ['A'], experimental: [], excluded: { B: '' }, notes: {} },
    { verified: ['A'], experimental: ['A'], excluded: {}, notes: {} },
    { verified: ['A'], experimental: ['B'], excluded: { B: 'why' }, notes: {} }]) {
    assert.equal(tiers.macTaskTiersShapeError(raw), tiers.MAC_TIERS_MALFORMED, JSON.stringify(raw));
  }
  assert.equal(tiers.macTaskTiersShapeError({
    verified: manifest.entries, experimental: manifest.experimental, excluded: manifest.excluded, notes: manifest.notes,
  }), '');
  // Even without the service's check, empty tiers on macOS never let a run start unmarked.
  assert.equal(tiers.macStartBlockedReason('macos', tiers.normalizeMacTaskTiers({})), tiers.MAC_TIERS_MALFORMED);
  assert.equal(tiers.macStartBlockedReason('windows', tiers.normalizeMacTaskTiers({})), '');
});

test('a scheduled slot waits for a delayed tier response instead of being refused', async () => {
  const store = { backendOS: 'macos', macTaskTiers: null };
  const listeners = new Set();
  const subscribe = (listener) => { listeners.add(listener); return () => listeners.delete(listener); };
  let settled = false;
  const waiting = tiers.macTaskTiersSettled(() => store, subscribe, 10000).then(() => { settled = true; });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(settled, false);
  assert.equal(tiers.macStartBlockedReason(store.backendOS, store.macTaskTiers), tiers.MAC_TIERS_PENDING);
  store.macTaskTiers = mac;  // the response arrives
  for (const listener of [...listeners]) listener();
  await waiting;
  assert.equal(listeners.size, 0);
  assert.equal(tiers.macStartBlockedReason(store.backendOS, store.macTaskTiers), '');  // the slot now starts
  // Other backends never wait; a response that never comes gives up and the gate says why.
  await tiers.macTaskTiersSettled(() => ({ backendOS: 'windows', macTaskTiers: null }), subscribe, 10000);
  const stuck = { backendOS: 'macos', macTaskTiers: null };
  await tiers.macTaskTiersSettled(() => stuck, subscribe, 5);
  assert.equal(listeners.size, 0);
  assert.equal(tiers.macStartBlockedReason(stuck.backendOS, stuck.macTaskTiers), tiers.MAC_TIERS_PENDING);
});

// The real ScheduleService, with its imports stubbed and the clock fixed to one minute.
function loadScheduler({ pending }) {
  const source = readFileSync(resolve(ROOT, 'client/src/services/scheduleService.ts'), 'utf8');
  const FIXED = new Date(2026, 10, 24, 21, 30, 10).getTime();
  class FixedDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(FIXED); }
    static now() { return FIXED; }
  }
  const storage = new Map();
  const tiers = { pending, release: null };
  const store = {
    instances: [{ id: 'B', name: 'B', isRunning: false,
      schedulePolicies: [{ enabled: true, name: 'p', weekdays: [new Date(FIXED).getDay()], times: ['21:30'] }] }],
  };
  const stubs = {
    '@/stores/appStore': { useAppStore: { getState: () => store } },
    '@/services/macTaskTiersService': {
      macTaskTiersPending: () => tiers.pending,
      waitForMacTaskTiers: () => new Promise((done) => { tiers.release = () => { tiers.pending = false; done(); }; }),
      // A native Mac instance: the per-instance Android check says no, and the Android wait is never reached.
      androidTaskTiersPendingFor: () => false,
      waitForAndroidTaskTiers: () => { throw new Error('a native Mac slot waited for the Android tiers'); },
    },
    '@/utils': { loggers: { task: { info() {}, warn() {}, error() {} } } },
  };
  const listeners = { addEventListener() {}, removeEventListener() {} };
  const context = {
    exports: {}, require: (id) => stubs[id], Date: FixedDate, setTimeout, clearTimeout, setInterval, clearInterval,
    localStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) },
    document: { ...listeners, visibilityState: 'visible' }, window: listeners, JSON, console,
  };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: 'scheduleService.ts' });
  const recorded = () => JSON.parse(storage.get('mxu_schedule_triggeredSlots') || '[]');
  return { service: context.exports.scheduleService, tiers, recorded };
}

const ticks = () => new Promise((done) => setTimeout(done, 5));

test('the scheduler never waits off macOS: the slot starts before a teardown, as before', async () => {
  const { service, recorded } = loadScheduler({ pending: false });
  const started = [];
  service.setTriggerCallback(async (inst) => { started.push(inst.id); return true; });
  service.start();  // its immediate check hands the slot over synchronously
  service.stop();
  service.setTriggerCallback(null);
  await ticks();
  assert.deepEqual(started, ['B']);
  assert.deepEqual(recorded(), ['B:2026-11-24-21-30']);
});

test('a scheduler stopped while waiting for the Mac tiers records nothing and a later start still runs the slot', async () => {
  const { service, tiers, recorded } = loadScheduler({ pending: true });
  const first = [];
  service.setTriggerCallback(async (inst) => { first.push(inst.id); return true; });
  service.start();
  await ticks();
  service.stop();
  service.setTriggerCallback(null);
  tiers.release();
  await ticks();
  assert.deepEqual(first, []);
  assert.deepEqual(recorded(), []);  // the slot is still free
  const second = [];
  service.setTriggerCallback(async (inst) => { second.push(inst.id); return true; });
  service.start();  // started again in the same minute, tiers loaded
  await ticks();
  service.stop();
  assert.deepEqual(second, ['B']);
  assert.deepEqual(recorded(), ['B:2026-11-24-21-30']);
});

test('a scheduler restarted while waiting for the Mac tiers runs the due slot once with the new handler', async () => {
  const { service, tiers, recorded } = loadScheduler({ pending: true });
  const first = [];
  const second = [];
  service.setTriggerCallback(async (inst) => { first.push(inst.id); return true; });
  service.start();
  await ticks();
  service.stop();
  service.setTriggerCallback(async (inst) => { second.push(inst.id); return true; });
  service.start();  // its immediate check is skipped: the first one still holds the slot decision
  await ticks();
  tiers.release();
  await ticks();
  await service.check();  // a later poll in the same minute does not run it again
  service.stop();
  assert.deepEqual(first, []);
  assert.deepEqual(second, ['B']);
  assert.deepEqual(recorded(), ['B:2026-11-24-21-30']);
});

test('a scheduler waiting for the Mac tiers starts the slot once they arrive', async () => {
  const { service, tiers, recorded } = loadScheduler({ pending: true });
  const started = [];
  service.setTriggerCallback(async (inst) => { started.push(inst.id); return true; });
  service.start();
  await ticks();
  assert.deepEqual(started, []);
  await service.check();  // an overlapping check while the first one waits does nothing
  tiers.release();
  await ticks();
  service.stop();
  assert.deepEqual(started, ['B']);
  assert.deepEqual(recorded(), ['B:2026-11-24-21-30']);
});
