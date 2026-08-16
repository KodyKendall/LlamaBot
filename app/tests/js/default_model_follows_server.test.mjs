// The 0.7.0 release blocker: /api/available-models correctly reported
// `default_model: muse-spark-1.2-contributor`, but every real browser turn
// still arrived as `llm_model: 'deepseek-v4-flash'` and was served by
// api.deepseek.com. The server default never got a say, because the frontend
// ALWAYS sent an explicit model taken from a dropdown whose initial selection
// was compile-time DeepSeek (`selected` in chat.html, DEFAULT_TEXT_MODEL and a
// send-path `|| 'deepseek-v4-flash'` in index.js).
//
// The rule under test: with no choice of the user's own, the dropdown FOLLOWS
// the server's default_model — and nothing in the frontend names a model id as
// the selection fallback, so the two can no longer drift apart.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

import { chooseInitialModel } from '../../frontend/chat/utils/modelDefaults.js';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');
const CHAT_HTML = readFileSync(resolve(APP_ROOT, 'frontend', 'chat.html'), 'utf8');

const MUSE = 'muse-spark-1.2-contributor';
const DEEPSEEK = 'deepseek-v4-flash';

/** The dropdown as it looks after fetchAvailableModels() applies availability. */
const dropdown = ({ museAvailable }) => [
  // The placeholder that holds the selection until the fetch resolves.
  { value: '', disabled: false },
  { value: 'gemini-3-flash', disabled: true },
  { value: DEEPSEEK, disabled: false },
  { value: MUSE, disabled: !museAvailable },
];

test('a box WITH a META key starts on the server default (Muse)', () => {
  assert.equal(
    chooseInitialModel({ options: dropdown({ museAvailable: true }), defaultModel: MUSE }),
    MUSE,
  );
});

test('a box WITHOUT a META key falls to the first available model', () => {
  // The server still reports Muse as the default there, but marks it
  // unavailable ("API key not configured") — the DeepSeek-fallback box.
  assert.equal(
    chooseInitialModel({ options: dropdown({ museAvailable: false }), defaultModel: MUSE }),
    DEEPSEEK,
  );
});

test('the empty placeholder is never selectable', () => {
  const onlyPlaceholder = [{ value: '', disabled: false }];
  assert.equal(chooseInitialModel({ options: onlyPlaceholder, defaultModel: MUSE }), null);
});

test('a disabled model is never chosen, even as first-available', () => {
  const allDisabled = [
    { value: '', disabled: false },
    { value: DEEPSEEK, disabled: true },
    { value: MUSE, disabled: true },
  ];
  assert.equal(chooseInitialModel({ options: allDisabled, defaultModel: MUSE }), null);
});

test('an unknown/absent server default still yields a working model', () => {
  // Older backend that omits default_model, or a default this build has no
  // <option> for: chat must still work rather than sitting on the placeholder.
  assert.equal(
    chooseInitialModel({ options: dropdown({ museAvailable: true }), defaultModel: null }),
    DEEPSEEK,
  );
});

test('nothing is pre-selected in the markup', () => {
  // A `selected` attribute on a real model is exactly the bug: it makes the
  // dropdown's default compile-time, so it disagrees with model_policy the
  // moment DEFAULT_LLM_MODEL changes.
  const select = CHAT_HTML.slice(
    CHAT_HTML.indexOf('data-llamabot="model-select"'),
    CHAT_HTML.indexOf('</select>', CHAT_HTML.indexOf('data-llamabot="model-select"')),
  );
  assert.ok(select.length > 0, 'model-select not found — did the markup move?');
  const preselected = select
    .split('\n')
    .filter(line => /<option/.test(line) && /\bselected\b/.test(line));
  assert.equal(preselected.length, 1, 'exactly one option may carry `selected`');
  assert.match(
    preselected[0],
    /value=""/,
    'only the empty placeholder may be pre-selected; a model id here overrides the server default',
  );
});

test('no model id survives as a selection fallback in the frontend', () => {
  // Grep-level sweep, same spirit as the cache-idiom sweep: the class of bug is
  // "a model id written down as what to use when we do not know", anywhere.
  assert.doesNotMatch(
    INDEX_JS,
    /\|\|\s*['"]deepseek-v4-flash['"]/,
    'send-path/selection fallbacks must use the resolved default, not a literal',
  );
  assert.doesNotMatch(
    INDEX_JS,
    /DEFAULT_TEXT_MODEL\s*=\s*['"]/,
    'there is no compile-time default model any more — it comes from the backend',
  );
});

test('the initial selection is driven by the backend value', () => {
  assert.match(INDEX_JS, /data\.default_model/);
  assert.match(INDEX_JS, /chooseInitialModel\(/);
  assert.match(INDEX_JS, /this\.userChoseModel/);
});

test("a user's own choice still wins and still persists", () => {
  // The three ways a user gets a choice of their own; each must flag it so the
  // fetch does not yank them back onto the default.
  assert.equal(
    (INDEX_JS.match(/this\.userChoseModel = true/g) || []).length,
    3,
    'cookie restore, ?llm_model= pin, and manual dropdown change',
  );
  // ...and the server default must NOT be written to the cookie, or today's
  // default freezes onto the user and a later fleet change never reaches them.
  const block = INDEX_JS.slice(
    INDEX_JS.indexOf('if (!this.userChoseModel) {'),
    INDEX_JS.indexOf('// If current selection is unavailable'),
  );
  assert.ok(block.length > 0, 'default-selection block not found');
  assert.doesNotMatch(block, /setCookie/);
});
