const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadOptions(controlBlock) {
  const calls = [], exports = [], hooks = [];
  const context = {
    api: {},
    nativeBuild: { option_release_rva: 0x167a60, option_release_control_block: controlBlock },
    qotModule: { base: { add: offset => ({ offset }) } },
    historyTransportReplyAddress: 'selected-build-reply-address',
    Interceptor: { attach: address => { hooks.push(address); return {}; } },
    exportAddress: prefix => { exports.push(prefix); return prefix; },
    NativeFunction: function(address) { return argument => { calls.push({ address, argument }); }; },
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'native_options.js'), 'utf8'), context);
  return { context, calls, exports, hooks };
}

test('16.33 cleanup passes the shared_ptr holder to its native destructor', () => {
  const { context, calls } = loadOptions(false);
  const holder = { isNull: () => false, add: () => { throw Error('Must not dereference legacy holder'); } };
  context.optionMetadataRelease(holder);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].argument, holder);
});

test('16.37 cleanup passes the control block and ignores empty owners', () => {
  const { context, calls } = loadOptions(true);
  const owner = { isNull: () => false };
  const holder = { add: offset => { assert.equal(offset, 8); return { readPointer: () => owner }; } };
  context.optionMetadataRelease(holder);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].argument, owner);
  context.optionMetadataRelease({ add: () => ({ readPointer: () => ({ isNull: () => true }) }) });
  assert.equal(calls.length, 1);
});

test('option replies hook the selected transport, and metadata resolves named exports', () => {
  const { exports, hooks } = loadOptions(true);
  assert.deepEqual(hooks, ['selected-build-reply-address']);
  assert.ok(exports.some(name => name.startsWith('?GetItem@IOptionListData@') && name.endsWith('_K_N@Z')));
  assert.ok(exports.includes('?GetOptionID@COptionData@option_list@@'));
  assert.ok(exports.includes('?GetFutuSymbol@COptionData@option_list@@'));
});
