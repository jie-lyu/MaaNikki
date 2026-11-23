/* Offline game target selector checks: node scripts/test_game_target.cjs.
 * Uses the client's installed TypeScript compiler and a fake window finder; never connects to the game.
 */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../client/node_modules/typescript');

const logs = [];
const modules = {
  '@/utils/logger': { loggers: new Proxy({}, { get: () => ({ warn: (...args) => logs.push(args) }) }) },
};

function load(path) {
  const source = readFileSync(resolve(__dirname, '..', 'client/src', path), 'utf8');
  const context = {
    exports: {},
    require: (name) => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected import: ${name}`);
      return modules[name];
    },
  };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: path });
  return context.exports;
}

modules['@/types/maa'] = load('types/maa.ts');
modules['@/utils/controller'] = load('utils/controller.ts');
const target = load('utils/gameTarget.ts');

const win32 = {
  name: 'nikki_win32', type: 'Win32', display_short_side: 720,
  win32: { class_regex: '^UnrealWindow$', window_regex: '^无限暖暖', screencap: 'FramePool', mouse: 'Seize', keyboard: 'Seize' },
};
const second = { ...win32, name: 'second_win32', win32: { class_regex: 'other', window_regex: 'other' } };
const pi = (...controller) => ({ controller });

function finder(result) {
  const calls = [];
  return {
    calls,
    async findWin32Windows(...args) {
      calls.push(args);
      return result;
    },
  };
}

test('selects the first Win32 controller and ignores other types', () => {
  const adb = { name: 'adb', type: 'Adb' };
  const mac = { name: 'mac', type: 'MacOS', macos: { title_regex: 'x' } };
  const gamepad = { name: 'pad', type: 'Gamepad', gamepad: { class_regex: 'x', window_regex: 'x' } };
  assert.equal(target.selectGameController(pi(adb, mac, gamepad, win32, second)), win32);
  assert.equal(target.selectGameController(pi(adb, mac, gamepad)), undefined);
  assert.equal(target.selectGameController(pi()), undefined);
  assert.equal(target.selectGameController(null), undefined);
  assert.equal(target.selectGameController(undefined), undefined);
});

test('passes the controller class/title regex and quiet flag to the finder', async () => {
  const windows = [{ handle: 42, class_name: 'UnrealWindow', window_name: '无限暖暖' }];
  const quiet = finder(windows);
  assert.equal(await target.findGameWindows(quiet, win32, true), windows);
  assert.deepEqual(quiet.calls, [['^UnrealWindow$', '^无限暖暖', true]]);

  // Services that omit the flag keep the finder's default (undefined -> quiet = false).
  const loud = finder(windows);
  assert.equal(await target.findGameWindows(loud, win32), windows);
  assert.deepEqual(loud.calls, [['^UnrealWindow$', '^无限暖暖', undefined]]);
});

test('returns the empty result when no window is found', async () => {
  const empty = finder([]);
  const windows = await target.findGameWindows(empty, win32);
  assert.deepEqual(windows, []);
  assert.equal(windows[0], undefined);
  assert.equal(empty.calls.length, 1);
});

test('builds the Win32 controller config with the found window handle', () => {
  const window = { handle: 4242, class_name: 'UnrealWindow', window_name: '无限暖暖' };
  const config = target.buildGameControllerConfig(win32, window);
  const expected = modules['@/utils/controller'].buildDesktopWindowControllerConfig(win32, 4242);
  assert.equal(config.type, 'Win32');
  assert.equal(config.handle, 4242);
  assert.equal(config.display_short_side, 720);
  assert.deepEqual(config, expected);
  assert.equal(target.buildGameControllerConfig(undefined, window), null);
});
