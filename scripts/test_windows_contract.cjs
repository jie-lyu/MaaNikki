/* Windows contract through the production client code: node scripts/test_windows_contract.cjs [--update].
 * Loads interface.json with the real autoLoadInterface() as a Windows backend (Tauri calls faked to read
 * this checkout), checks it equals the effective interface that scripts/test_windows_contract.py pins,
 * and pins the Win32 controller config built by the real buildDesktopWindowControllerConfig().
 * Needs the client's installed dependencies and a Python 3.11+ on PATH (or $PYTHON). Never contacts a game.
 */
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const { readFileSync, writeFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../client/node_modules/typescript');

const ROOT = resolve(__dirname, '..');
const CONFIG_FIXTURE = resolve(ROOT, 'scripts/fixtures/windows-controller-config.json');
const HANDLE = 0x1234;
const quiet = () => {};
const logger = new Proxy({}, { get: () => ({ info: quiet, warn: quiet, error: quiet, debug: quiet }) });

const modules = {
  '@/utils/logger': { loggers: logger },
  './logger': { loggers: logger },
  'jsonc-parser': require('../client/node_modules/jsonc-parser'),
  '@/utils/paths': { isTauri: () => true },
  '@/utils/backendApi': { setBackendPort: quiet },
  '@tauri-apps/api/core': {
    invoke: async (command, args) => {
      switch (command) {
        case 'get_exe_dir':
        case 'get_data_dir':
          return ROOT;
        case 'read_local_file':
          return readFileSync(resolve(ROOT, args.filename), 'utf8');
        case 'get_os':
          return 'windows';
        case 'get_arch':
          return 'x86_64';
        case 'get_web_server_port':
          return 0;
        default:
          throw new Error(`Unexpected invoke: ${command}`);
      }
    },
  },
};

function load(path) {
  const source = readFileSync(resolve(ROOT, 'client/src', path), 'utf8');
  const context = {
    exports: {},
    console,
    navigator: { platform: 'Win32' },
    require: (name) => {
      assert.ok(Object.hasOwn(modules, name), `Unexpected import from ${path}: ${name}`);
      return modules[name];
    },
  };
  vm.runInNewContext(ts.transpileModule(source, {
    compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
  }).outputText, context, { filename: path });
  return context.exports;
}

modules['@/types/interface'] = load('types/interface.ts');
modules['@/types/maa'] = load('types/maa.ts');
modules['@/utils/jsonc'] = load('utils/jsonc.ts');
const loader = load('services/interfaceLoader.ts');
const controllerUtils = load('utils/controller.ts');

function pythonEffectiveInterface() {
  const code = [
    'import importlib.util, json, sys',
    `spec = importlib.util.spec_from_file_location('contract', ${JSON.stringify(resolve(ROOT, 'scripts/test_windows_contract.py'))})`,
    'module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)',
    'sys.stdout.reconfigure(encoding="utf-8")',
    'print(json.dumps(module.effective_interface(), ensure_ascii=False))',
  ].join('\n');
  const candidates = process.env.PYTHON ? [process.env.PYTHON] : ['python', 'python3'];
  for (const python of candidates) {
    try {
      return JSON.parse(execFileSync(python, ['-B', '-c', code], { encoding: 'utf8' }));
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  throw new Error('Python not found; set $PYTHON');
}

let loaded;
async function windowsInterface() {
  loaded ??= await loader.autoLoadInterface();
  return loaded;
}

test('production loader on a Windows backend matches the pinned effective interface', async () => {
  const result = await windowsInterface();
  assert.equal(result.backendOS, 'windows');
  assert.deepStrictEqual(JSON.parse(JSON.stringify(result.interface)), pythonEffectiveInterface());
});

test('Windows sees only nikki_windows and builds the pinned Win32 controller config', async () => {
  const pi = (await windowsInterface()).interface;
  assert.deepStrictEqual(pi.controller.map((item) => item.name), ['nikki_windows']);
  const configs = Object.fromEntries(pi.controller.map((item) => [
    item.name, controllerUtils.buildDesktopWindowControllerConfig(item, HANDLE),
  ]));
  const actual = JSON.parse(JSON.stringify(configs));
  if (process.argv.includes('--update')) {
    writeFileSync(CONFIG_FIXTURE, `${JSON.stringify(actual, null, 2)}\n`);
  }
  assert.deepStrictEqual(actual, JSON.parse(readFileSync(CONFIG_FIXTURE, 'utf8')),
    'Win32 controller config changed; rerun with --update if intended and review the diff');
});
