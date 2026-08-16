// `path.replace is not a function` — 4 occurrences across leo-nefe and
// leo-mezuli on 0.7.0, thrown out of socket.onmessage, which kills the chat
// panel rather than dropping one message.
//
// _extractFilename type-guarded only the `bundle exec` branch and then called
// .replace() unconditionally, while _extractDisplayTarget hands it the tool
// call's FIRST ARGUMENT verbatim — so any tool whose first argument is a
// number, object, array or boolean threw. The delegate branch has the same bug
// in its silent form: `.length` on a non-string is undefined, so it renders the
// wrong thing instead of crashing.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

// The renderer reads document.querySelector to detect beginner/plan mode, and
// PlanMessageRenderer registers globals on window at import time.
globalThis.window = globalThis;
globalThis.document = {
  querySelector: () => null,
  createElement: () => makeElement(),
  addEventListener: () => {},
};

function makeElement() {
  return {
    innerHTML: '',
    className: '',
    id: '',
    _attrs: {},
    setAttribute(key, value) { this._attrs[key] = value; },
    getAttribute(key) { return this._attrs[key]; },
    appendChild() {},
  };
}

const { ToolMessageRenderer } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'messages', 'ToolMessageRenderer.js')
);
const { MessageRenderer } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'messages', 'MessageRenderer.js')
);

const renderer = () => new ToolMessageRenderer();

// [what the tool passed as its first argument, what should be displayed]
const NON_STRINGS = [
  [42, '42'],
  [0, ''],                       // falsy — the early return already covers it
  [true, 'true'],
  [['a', 'b'], 'a,b'],
  [{ file_path: 'app/models/user.rb' }, 'user.rb'],
  [{ path: 'db/schema.rb' }, 'schema.rb'],
];

for (const [firstArgument, expected] of NON_STRINGS) {
  test(`_extractFilename survives ${JSON.stringify(firstArgument)}`, () => {
    const value = renderer()._extractFilename(firstArgument);
    assert.equal(typeof value, 'string');
    assert.equal(value, expected);
  });
}

test('_extractFilename still handles the shapes it always did', () => {
  const r = renderer();
  assert.equal(r._extractFilename('app/models/user.rb'), 'user.rb');
  assert.equal(r._extractFilename('app\\models\\user.rb'), 'user.rb');
  assert.equal(r._extractFilename('bundle exec rspec spec/'), 'rspec spec/');
  assert.equal(r._extractFilename(null), '');
  assert.equal(r._extractFilename(undefined), '');
});

test('_extractDisplayTarget never throws, whatever the tool passed', () => {
  const r = renderer();
  for (const [firstArgument] of NON_STRINGS) {
    assert.equal(typeof r._extractDisplayTarget('read_file', firstArgument), 'string');
    assert.equal(typeof r._extractDisplayTarget('delegate_task', firstArgument), 'string');
  }
});

test('a long delegate task is truncated, and a non-string one does not fake it', () => {
  const r = renderer();
  const long = 'x'.repeat(120);
  assert.equal(r._extractDisplayTarget('delegate_task', long), 'x'.repeat(60) + '...');
  // `.length` on an object is undefined, so the old code fell through and
  // returned the object itself — which the DOM then rendered as [object Object].
  const target = r._extractDisplayTarget('delegate_task', { task: 'do a thing' });
  assert.equal(typeof target, 'string');
  assert.ok(!target.includes('[object Object]'), target);
});

test('one malformed tool payload does not take down the message renderer', () => {
  // Belt and braces: even if some future tool payload breaks the tool renderer,
  // the throw must not escape into socket.onmessage and kill the panel.
  const messageRenderer = Object.create(MessageRenderer.prototype);
  messageRenderer.markdownParser = { parse: (s) => s };
  messageRenderer.config = {};
  messageRenderer.insertMessage = () => {};
  messageRenderer.toolRenderer = {
    createCollapsibleToolMessage() {
      throw new TypeError('path.replace is not a function');
    },
  };

  const div = messageRenderer.renderAiMessage('', {
    tool_calls: [{ name: 'read_file', args: { file_path: 7 }, id: 'call_1' }],
  });

  assert.ok(div, 'renderAiMessage must still return a message element');
  assert.equal(div.id, 'call_1');
});
