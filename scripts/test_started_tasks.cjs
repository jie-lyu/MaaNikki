/* Offline checks for naming started tasks: node scripts/test_started_tasks.cjs.
 * Uses the client's installed TypeScript compiler; no client, agent or game.
 */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const ts = require('../client/node_modules/typescript');

function load(path) {
  const source = readFileSync(resolve(__dirname, '..', 'client/src', path), 'utf8');
  const context = { exports: {} };
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
