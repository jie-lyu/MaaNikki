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

/** A store whose Android tiers record every read: the native Mac paths must never read them. */
function watchAndroidReads(store) {
  const reads = [];
  let value = store.androidTaskTiers;
  delete store.androidTaskTiers;
  Object.defineProperty(store, 'androidTaskTiers', {
    get() { reads.push(new Error().stack); return value; },
    set(next) { value = next; },
    enumerable: true,
  });
  return reads;
}

test('native Mac scheduling waits for the Mac tiers alone and never reads the Android tiers', async () => {
  const instance = (id, controllerName) => ({ id, name: id, controllerName });
  for (const androidTaskTiers of [null, { ...androidTiers, error: 'broken' }]) {  // pending or failed
    const { service, store, notify } = loadService({
      state: { androidTaskTiers, instances: [instance('M', 'mac'), instance('A', 'nikki_android')] },
    });
    const reads = watchAndroidReads(store);
    assert.equal(service.macTaskTiersPending(), true);  // exactly the Mac check, an Adb instance present or not
    const waiting = service.waitForMacTaskTiers(10000);
    store.macTaskTiers = macTiers;
    notify();
    await waiting;
    assert.equal(service.macTaskTiersPending(), false);
    assert.equal(service.androidTaskTiersPendingFor('M'), false);
    assert.equal(reads.length, 0, 'a native Mac path read the Android tiers');
  }
  // Off macOS nothing is ever pending, and the Android tiers are not read either.
  for (const os of ['windows', 'linux', '']) {
    const { service, store } = loadService({ state: { backendOS: os, instances: [instance('A', 'nikki_android')] } });
    const reads = watchAndroidReads(store);
    assert.equal(service.macTaskTiersPending(), false);
    assert.equal(service.androidTaskTiersPendingFor('A'), false);
    assert.equal(reads.length, 0);
  }
});

test('a due Adb instance on macOS waits for the Android tiers, chosen by its selected controller', async () => {
  const instance = (id, controllerName) => ({ id, name: id, controllerName });
  const { service, store, notify, listeners } = loadService({
    state: { macTaskTiers: macTiers, instances: [instance('A', 'mac')], selectedController: { A: 'nikki_android' } },
  });
  assert.equal(service.instanceControllerType(store, 'A'), 'Adb');  // selectedController first, as in the store
  assert.equal(service.androidTaskTiersPendingFor('A'), true);
  let done = false;
  const waiting = service.waitForAndroidTaskTiers(10000).then(() => { done = true; });
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(done, false);
  store.androidTaskTiers = { ...androidTiers, error: 'unreadable' };  // a failed load settles it too
  notify();
  await waiting;
  assert.equal(listeners.size, 0);
  assert.equal(service.androidTaskTiersPendingFor('A'), false);
});

// The real ScheduleService over the real tiers service: one Mac and one Adb instance due in the same minute.
function loadSchedulerWith(store, subscribe) {
  const FIXED = new Date(2026, 10, 24, 21, 30, 10).getTime();
  class FixedDate extends Date {
    constructor(...args) { if (args.length) super(...args); else super(FIXED); }
    static now() { return FIXED; }
  }
  const policy = { enabled: true, name: 'p', weekdays: [new Date(FIXED).getDay()], times: ['21:30'] };
  for (const inst of store.instances) inst.schedulePolicies = [policy];
  const useAppStore = { getState: () => store, subscribe };
  const helpers = load('stores/helpers.ts', { '@/utils/optionHelpers': { findSwitchCase() {} } });
  const tiersService = load('services/macTaskTiersService.ts', {
    '@tauri-apps/api/core': { invoke() {} }, '@/utils/backendApi': { apiGet() {} }, '@/utils/paths': { isTauri: () => true },
    '@/utils/logger': { loggers: { app: { warn() {} } } }, '@/stores/appStore': { useAppStore },
    '@/stores/helpers': helpers, '@/utils/macTaskTiers': mac, '@/utils/taskTiers': tiers,
  });
  const storage = new Map();
  const listeners = { addEventListener() {}, removeEventListener() {} };
  const context = {
    exports: {}, Date: FixedDate, setTimeout, clearTimeout, setInterval, clearInterval, JSON, console,
    require: (id) => ({ '@/stores/appStore': { useAppStore }, '@/services/macTaskTiersService': tiersService,
      '@/utils': { loggers: { task: { info() {}, warn() {}, error() {} } } } })[id],
    localStorage: { getItem: (k) => storage.get(k) ?? null, setItem: (k, v) => storage.set(k, String(v)) },
    document: { ...listeners, visibilityState: 'visible' }, window: listeners,
  };
  vm.runInNewContext(ts.transpileModule(readFileSync(resolve(ROOT, 'client/src/services/scheduleService.ts'), 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: 'scheduleService.ts' });
  return context.exports.scheduleService;
}

test('the scheduler starts a due native Mac instance at once while an Adb instance waits for the Android tiers', async () => {
  const subscribers = new Set();
  const subscribe = (listener) => { subscribers.add(listener); return () => subscribers.delete(listener); };
  const store = {
    backendOS: 'macos', macTaskTiers: macTiers, androidTaskTiers: null, selectedController: {}, selectedResource: {},
    projectInterface: { controller: [{ name: 'mac', type: 'MacOS' }, { name: 'nikki_android', type: 'Adb' }], resource: [] },
    instances: [{ id: 'M', name: 'M', controllerName: 'mac', isRunning: false },
                { id: 'A', name: 'A', controllerName: 'nikki_android', isRunning: false }],
  };
  const reads = watchAndroidReads(store);
  const service = loadSchedulerWith(store, subscribe);
  const started = [];
  service.setTriggerCallback(async (inst) => { started.push([inst.id, reads.length]); return true; });
  service.start();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(started, [['M', 0]]);  // the Mac slot ran before anything looked at the Android tiers
  store.androidTaskTiers = androidTiers;
  for (const listener of [...subscribers]) listener();
  await new Promise((resolve) => setTimeout(resolve, 5));
  service.stop();
  assert.deepEqual(started.map(([id]) => id), ['M', 'A']);
});

test('the scheduler runs a due native Mac instance listed after an Adb one even if stopped during the Android wait', async () => {
  const subscribers = new Set();
  const subscribe = (listener) => { subscribers.add(listener); return () => subscribers.delete(listener); };
  const store = {
    backendOS: 'macos', macTaskTiers: macTiers, androidTaskTiers: null, selectedController: {}, selectedResource: {},
    projectInterface: { controller: [{ name: 'mac', type: 'MacOS' }, { name: 'nikki_android', type: 'Adb' }], resource: [] },
    instances: [{ id: 'A', name: 'A', controllerName: 'nikki_android', isRunning: false },
                { id: 'M', name: 'M', controllerName: 'mac', isRunning: false }],
  };
  const reads = watchAndroidReads(store);
  const service = loadSchedulerWith(store, subscribe);
  const started = [];
  service.setTriggerCallback(async (inst) => { started.push([inst.id, reads.length]); return true; });
  service.start();
  await new Promise((resolve) => setTimeout(resolve, 5));
  service.stop();  // stopped during the Android tier wait
  // Mac was decided and started without waiting; the only Android tier read is the Adb instance's own check.
  assert.deepEqual(started, [['M', 1]]);
  store.androidTaskTiers = androidTiers;
  for (const listener of [...subscribers]) listener();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(started.map(([id]) => id), ['M']);  // the stopped scheduler starts nothing more
});

test('an Adb schedule disabled during the Android tier wait is not started; the store is read again after it', async () => {
  const subscribers = new Set();
  const subscribe = (listener) => { subscribers.add(listener); return () => subscribers.delete(listener); };
  const store = {
    backendOS: 'macos', macTaskTiers: macTiers, androidTaskTiers: null, selectedController: {}, selectedResource: {},
    projectInterface: { controller: [{ name: 'mac', type: 'MacOS' }, { name: 'nikki_android', type: 'Adb' }], resource: [] },
    instances: [{ id: 'A', name: 'A', controllerName: 'nikki_android', isRunning: false },
                { id: 'B', name: 'B', controllerName: 'nikki_android', isRunning: false }],
  };
  const service = loadSchedulerWith(store, subscribe);
  const started = [];
  service.setTriggerCallback(async (inst) => { started.push(inst.id); return true; });
  service.start();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(started, []);
  // During the wait: A's policy is disabled (a fresh instance object, as the store writes it); B keeps its own.
  store.instances = [{ ...store.instances[0], schedulePolicies: [{ ...store.instances[0].schedulePolicies[0], enabled: false }] },
                     store.instances[1]];
  store.androidTaskTiers = androidTiers;
  for (const listener of [...subscribers]) listener();
  await new Promise((resolve) => setTimeout(resolve, 5));
  service.stop();
  assert.deepEqual(started, ['B']);
});

test('startup with only native controllers never requests the Android manifest; an Adb selection loads it once', async () => {
  for (const [os, controllers] of [
    ['macos', [{ name: 'mac', type: 'MacOS' }]],
    ['macos', [{ name: 'win', type: 'Win32' }]],
    ['macos', [{ name: 'mac', type: 'MacOS' }, { name: 'win', type: 'Win32' }]],
    ['windows', [{ name: 'win', type: 'Win32' }]],
  ]) {
    const { service, store, calls, notify } = loadService({
      invoke: () => view(androidManifest), state: { backendOS: '', projectInterface: null, instances: [] },
    });
    const reads = watchAndroidReads(store);
    // Startup as App.loadInterface does it: the backend OS first, then the interface and the saved instances.
    store.backendOS = os;
    if (os === 'macos') service.watchAndroidTaskTiersNeed();
    store.projectInterface = { controller: controllers, resource: [] };
    notify();
    store.instances = [{ id: 'X', name: 'X' }, { id: 'Y', name: 'Y', controllerName: controllers.at(-1).name }];
    notify();
    store.selectedController = { X: controllers[0].name };
    notify();
    await new Promise((resolve) => setTimeout(resolve, 5));
    assert.deepEqual(calls, [], `${os} ${controllers.map((c) => c.type)} requested the Android manifest`);
    assert.equal(reads.length, 0);
  }
  // An instance that selects the Adb controller loads it, exactly once, and only then.
  const { service, store, calls, notify } = loadService({
    invoke: () => view(androidManifest), state: { instances: [{ id: 'X', name: 'X', controllerName: 'mac' }] },
  });
  store.setAndroidTaskTiers = (tiers) => { store.androidTaskTiers = tiers; };
  service.watchAndroidTaskTiersNeed();
  notify();
  assert.deepEqual(calls, []);
  store.selectedController = { X: 'nikki_android' };
  notify();
  notify();
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.deepEqual(calls, ['get_android_task_tiers']);
  assert.deepEqual(plain(store.androidTaskTiers), plain(androidTiers));
  // App.tsx itself no longer loads the Android manifest at startup.
  const app = readFileSync(resolve(ROOT, 'client/src/App.tsx'), 'utf8');
  assert.equal(app.includes('loadAndroidTaskTiers('), false);
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
