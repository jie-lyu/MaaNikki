/* Offline checks for the per-instance task tier choice (Mac vs Android emulator): node scripts/test_task_tiers.cjs.
 * Uses the client's installed TypeScript compiler and the shipped manifests; no client, agent, game or device.
 */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../client/node_modules/typescript');

const ROOT = resolve(__dirname, '..');

function load(path, stubs = {}) {
  const source = readFileSync(resolve(ROOT, 'client/src', path), 'utf8');
  const context = { exports: {}, require: (id) => stubs[id], setTimeout, clearTimeout, Error, JSON };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: path });
  return context.exports;
}

const mac = load('utils/macTaskTiers.ts');
const tiers = load('utils/taskTiers.ts', { '@/utils/macTaskTiers': mac });
const plain = (value) => JSON.parse(JSON.stringify(value));

// What the backend returns (commands::utils::task_tiers_view): entries become the verified list.
const view = (manifest) => ({
  verified: manifest.entries, experimental: manifest.experimental, excluded: manifest.excluded, notes: manifest.notes,
});
const macManifest = JSON.parse(readFileSync(resolve(ROOT, 'agent/host/macos/preview_tasks.json'), 'utf8'));
const androidManifest = JSON.parse(readFileSync(resolve(ROOT, 'agent/host/android/tasks.json'), 'utf8'));
const macTiers = mac.normalizeMacTaskTiers(view(macManifest));
const androidTiers = mac.normalizeMacTaskTiers(view(androidManifest));

const ENTRIES = ['MaaNikki_Mailbox', 'MaaNikki_AndroidCheck', 'MaaNikki_StartGame', 'MaaNikki_Home', 'MXU_LAUNCH', '', undefined];

test('the tier list follows the controller: Adb on macOS gets the Android tiers, everything else the Mac tiers', () => {
  assert.equal(tiers.tiersForController('macos', 'Adb', macTiers, androidTiers), androidTiers);
  for (const type of ['MacOS', 'Win32', 'PlayCover', undefined, '']) {
    assert.equal(tiers.tiersForController('macos', type, macTiers, androidTiers), macTiers);
    assert.equal(tiers.tierPlatformFor('macos', type), 'mac');
  }
  assert.equal(tiers.tierPlatformFor('macos', 'Adb'), 'android');
  assert.equal(tiers.tiersForController('macos', 'Adb', macTiers, null), null);
  // Off macOS the Mac tiers are what is passed (null there, never loaded), whatever the controller.
  for (const os of ['windows', 'linux', '']) {
    for (const type of ['Adb', 'Win32', 'MacOS', undefined]) {
      assert.equal(tiers.tierPlatformFor(os, type), 'mac');
      assert.equal(tiers.tiersForController(os, type, null, androidTiers), null);
    }
  }
});

test('Mac marks, wording and start gate are exactly the macTaskTiers ones', () => {
  for (const source of [macTiers, null, undefined, mac.normalizeMacTaskTiers({ error: 'x' })]) {
    for (const entry of ENTRIES) {
      const mark = tiers.taskMarkFor('mac', source, entry);
      assert.deepEqual(plain(mark), plain(mac.macTaskMark(source, entry)));
      assert.equal(tiers.taskExcludedText(mark), mac.macExcludedText(mac.macTaskMark(source, entry)));
      assert.equal(tiers.experimentalBadgeText(mark), mac.MAC_EXPERIMENTAL_BADGE);
      assert.equal(tiers.experimentalHintText(mark), mac.MAC_EXPERIMENTAL_HINT);
    }
  }
  const broken = mac.normalizeMacTaskTiers({ error: '无法读取 Mac 任务清单（x）' });
  for (const os of ['macos', 'windows', 'linux', '']) {
    for (const type of ['MacOS', 'Win32', undefined]) {
      for (const source of [null, undefined, macTiers, broken, mac.normalizeMacTaskTiers({})]) {
        assert.equal(tiers.startBlockedReasonFor(os, type, source, androidTiers), mac.macStartBlockedReason(os, source));
        assert.equal(tiers.startBlockedReasonFor(os, type, source, null), mac.macStartBlockedReason(os, source));
      }
    }
  }
  // Off macOS an Adb instance is not gated either, as before.
  for (const os of ['windows', 'linux', '']) assert.equal(tiers.startBlockedReasonFor(os, 'Adb', null, null), '');
});

test('Android marks use the Android manifest and wording', () => {
  const check = tiers.taskMarkFor('android', androidTiers, 'MaaNikki_AndroidCheck');
  assert.deepEqual(plain(check), { tier: 'experimental', note: androidManifest.notes.MaaNikki_AndroidCheck, platform: 'android' });
  assert.equal(tiers.experimentalBadgeText(check), '安卓模拟器未验证（试用）');
  assert.equal(tiers.experimentalHintText(check), tiers.ANDROID_EXPERIMENTAL_HINT);
  assert.equal(tiers.taskExcludedText(check), '');
  assert.equal(tiers.taskMarkFor('android', androidTiers, 'MaaNikki_Mailbox').tier, 'experimental');
  // Entries in no Android list are refused with the Android reason.
  for (const entry of ['MaaNikki_StartGame', 'MaaNikki_Home', 'MXU_LAUNCH']) {
    const mark = tiers.taskMarkFor('android', androidTiers, entry);
    assert.deepEqual(plain(mark), { tier: 'excluded', reason: '暂不支持安卓模拟器。', platform: 'android' });
    assert.equal(tiers.taskExcludedText(mark), '安卓不可用：暂不支持安卓模拟器。');
  }
  const withReason = mac.normalizeMacTaskTiers({ verified: ['A'], experimental: [], excluded: { B: '需要键盘' }, notes: { A: 'n' } });
  assert.deepEqual(plain(tiers.taskMarkFor('android', withReason, 'A')), { tier: 'verified', note: 'n', platform: 'android' });
  assert.equal(tiers.taskExcludedText(tiers.taskMarkFor('android', withReason, 'B')), '安卓不可用：需要键盘');
  for (const source of [null, undefined, mac.normalizeMacTaskTiers({ error: 'x' })]) {
    for (const entry of ENTRIES) assert.equal(tiers.taskMarkFor('android', source, entry), null);
  }
  assert.equal(tiers.taskMarkFor('android', androidTiers, ''), null);
  assert.equal(tiers.taskExcludedText(null), '');
});

test('an Adb instance on macOS waits for and checks the Android tiers only', () => {
  assert.equal(tiers.startBlockedReasonFor('macos', 'Adb', macTiers, null), '安卓任务清单还没读取完，请稍后再开始。');
  assert.equal(tiers.startBlockedReasonFor('macos', 'Adb', null, undefined), tiers.ANDROID_TIERS_PENDING);
  assert.equal(tiers.startBlockedReasonFor('macos', 'Adb', null, androidTiers), '');  // Mac tiers do not matter
  const broken = mac.normalizeMacTaskTiers({ error: '无法读取安卓任务清单（x）' });
  assert.equal(tiers.startBlockedReasonFor('macos', 'Adb', macTiers, broken), '无法读取安卓任务清单（x）');
  assert.equal(tiers.startBlockedReasonFor('macos', 'Adb', macTiers, mac.normalizeMacTaskTiers({})), tiers.ANDROID_TIERS_MALFORMED);
  assert.equal(mac.macTaskTiersShapeError(view(androidManifest)), '');  // the shipped manifest is well formed
});

test('whenSettled resolves at once, on the change that settles it, or at the timeout', async () => {
  const listeners = new Set();
  const subscribe = (listener) => { listeners.add(listener); return () => listeners.delete(listener); };
  await tiers.whenSettled(() => true, subscribe, 10000);
  assert.equal(listeners.size, 0);
  let ready = false;
  let settled = false;
  const waiting = tiers.whenSettled(() => ready, subscribe, 10000).then(() => { settled = true; });
  for (const listener of [...listeners]) listener();  // a change that does not settle it
  await new Promise((done) => setTimeout(done, 5));
  assert.equal(settled, false);
  ready = true;
  for (const listener of [...listeners]) listener();
  await waiting;
  assert.equal(listeners.size, 0);
  await tiers.whenSettled(() => false, subscribe, 5);
  assert.equal(listeners.size, 0);
});

// The real service with its imports stubbed: the loader and the scheduler's wait.
function loadService({ state = {}, invoke, apiGet, tauri = true } = {}) {
  const store = {
    backendOS: 'macos', macTaskTiers: null, androidTaskTiers: null, instances: [], selectedController: {},
    selectedResource: {}, projectInterface: { controller: [{ name: 'mac', type: 'MacOS' }, { name: 'nikki_android', type: 'Adb' }], resource: [] },
    ...state,
  };
  const listeners = new Set();
  const useAppStore = {
    getState: () => store,
    subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
  };
  const helpers = load('stores/helpers.ts', { '@/utils/optionHelpers': { findSwitchCase() {} } });
  const calls = [];
  const service = load('services/macTaskTiersService.ts', {
    '@tauri-apps/api/core': { invoke: async (cmd) => { calls.push(cmd); return invoke(cmd); } },
    '@/utils/backendApi': { apiGet: async (path) => { calls.push(path); return apiGet(path); } },
    '@/utils/paths': { isTauri: () => tauri },
    '@/utils/logger': { loggers: { app: { warn() {} } } },
    '@/stores/appStore': { useAppStore },
    '@/stores/helpers': helpers,
    '@/utils/macTaskTiers': mac,
    '@/utils/taskTiers': tiers,
  });
  const notify = () => { for (const listener of [...listeners]) listener(); };
  return { service, store, calls, notify, listeners };
}

test('the Android loader reads its own command and route and words its errors for Android', async () => {
  const ok = loadService({ invoke: () => view(androidManifest) });
  assert.deepEqual(plain(await ok.service.loadAndroidTaskTiers()), plain(androidTiers));
  assert.deepEqual(ok.calls, ['get_android_task_tiers']);
  const web = loadService({ tauri: false, apiGet: () => view(androidManifest) });
  await web.service.loadAndroidTaskTiers();
  assert.deepEqual(web.calls, ['/android/task-tiers']);
  const empty = loadService({ invoke: () => ({ verified: [], experimental: [], excluded: {}, notes: {} }) });
  assert.equal((await empty.service.loadAndroidTaskTiers()).error, tiers.ANDROID_TIERS_MALFORMED);
  const failing = loadService({ invoke: () => { throw new Error('boom'); } });
  assert.equal((await failing.service.loadAndroidTaskTiers()).error, '无法读取安卓任务清单（boom）');
  // The Mac loader is unchanged: its own command and Mac wording.
  const macOnly = loadService({ invoke: () => ({}) });
  assert.equal((await macOnly.service.loadMacTaskTiers()).error, mac.MAC_TIERS_MALFORMED);
  assert.deepEqual(macOnly.calls, ['get_macos_task_tiers']);
});

test('the scheduler waits for the Android tiers only on macOS with an Adb instance', async () => {
  const instance = (id, controllerName) => ({ id, name: id, controllerName });
  // Off macOS: never pending, whatever is selected.
  for (const os of ['windows', 'linux', '']) {
    const { service } = loadService({ state: { backendOS: os, instances: [instance('A', 'nikki_android')] } });
    assert.equal(service.macTaskTiersPending(), false);
  }
  // macOS without an Adb instance: the Mac check alone, as before.
  const macOnly = loadService({ state: { instances: [instance('M', 'mac')] } });
  assert.equal(macOnly.service.macTaskTiersPending(), true);
  macOnly.store.macTaskTiers = macTiers;
  assert.equal(macOnly.service.macTaskTiersPending(), false);  // the Android tiers are not awaited
  await macOnly.service.waitForMacTaskTiers(10000);
  // macOS with an Adb instance: both.
  const both = loadService({ state: { instances: [instance('M', 'mac'), instance('A', 'nikki_android')] } });
  let done = false;
  const waiting = both.service.waitForMacTaskTiers(10000).then(() => { done = true; });
  both.store.macTaskTiers = macTiers;
  both.notify();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(done, false);
  assert.equal(both.service.macTaskTiersPending(), true);
  both.store.androidTaskTiers = androidTiers;
  both.notify();
  await waiting;
  assert.equal(both.listeners.size, 0);
  assert.equal(both.service.macTaskTiersPending(), false);
  // The selected controller (selectedController first) decides, as in the rest of the store.
  const switched = loadService({ state: { macTaskTiers: macTiers, instances: [instance('A', 'mac')], selectedController: { A: 'nikki_android' } } });
  assert.equal(switched.service.instanceControllerType(switched.store, 'A'), 'Adb');
  assert.equal(switched.service.macTaskTiersPending(), true);
});

test('a controller attach_resource_path loads after the resource paths; none adds nothing', () => {
  const paths = load('utils/resourcePath.ts');
  const resource = { name: 'r', path: ['./resource', './resource_extra'] };
  assert.deepEqual(plain(paths.computeResourcePaths(resource, { name: 'a', type: 'Adb', attach_resource_path: ['./resource_android'] }, '/base')),
    ['/base/resource', '/base/resource_extra', '/base/resource_android']);
  for (const controller of [{ name: 'm', type: 'MacOS' }, { name: 'w', type: 'Win32' }, undefined]) {
    assert.deepEqual(plain(paths.computeResourcePaths(resource, controller, '/base')), ['/base/resource', '/base/resource_extra']);
  }
});
