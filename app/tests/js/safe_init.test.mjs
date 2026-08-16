// leo-tama, 0.7.0: 18 occurrences over two days, the two biggest customer
// clusters in the release.
//
//   TypeError: this.fileAttachmentManager.initAssetModal is not a function
//     at ChatApp.initComponents (…/index.js?v=6:467:32)
//     at ChatApp.init (…/index.js?v=6:217:12)
//
//   this.elementSelector?.getSelectedElements is not a function
//     at ChatApp.sendMessage (…/index.js?v=6:1535:52)
//
// The shipped bundle is self-consistent — a browser ran a new index.js against
// an older cached module, because chat.html cache-busts only the entry point
// while its 36 imports carry no version stamp. Stamping the whole graph is the
// real fix; these two guards are the part that stops a skewed module from
// killing the chat panel outright:
//
//   - one failed init step logs and the rest of init still runs
//   - a missing getSelectedElements never blocks a send (selected elements are
//     optional context). `?.` does not help here: it guards a null object, not
//     a missing method on a live one.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { safeInit, selectedElementsOf } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'utils', 'safeInit.js')
);

test('a step that throws does not stop the steps after it', () => {
  const ran = [];
  const logged = [];
  const log = (...args) => logged.push(args);

  safeInit('managers', () => ran.push('managers'), log);
  safeInit('attachments', () => {
    throw new TypeError('this.fileAttachmentManager.initAssetModal is not a function');
  }, log);
  safeInit('slash commands', () => ran.push('slash commands'), log);

  assert.deepEqual(ran, ['managers', 'slash commands']);
  assert.equal(logged.length, 1, 'the failure must still be reported');
  assert.ok(String(logged[0][0]).includes('attachments'), logged[0][0]);
});

test('safeInit reports whether the step succeeded', () => {
  assert.equal(safeInit('ok', () => {}, () => {}), true);
  assert.equal(safeInit('bad', () => { throw new Error('nope'); }, () => {}), false);
});

test('selectedElementsOf tolerates every broken shape of the selector', () => {
  const cases = [
    undefined,
    null,
    {},                                        // module skew: object exists, method does not
    { getSelectedElements: 'not a function' },
    { getSelectedElements: () => null },
    { getSelectedElements: () => { throw new Error('boom'); } },
  ];

  for (const selector of cases) {
    assert.deepEqual(selectedElementsOf(selector), [], JSON.stringify(selector));
  }
});

test('selectedElementsOf still returns real selections', () => {
  const selected = [{ selector: '#hero' }];
  assert.deepEqual(selectedElementsOf({ getSelectedElements: () => selected }), selected);
});
