// The image auto-switch target is box-dependent (0.7.5).
//
// Until now `index.js` hardcoded `const IMAGE_MODEL = 'muse-spark-1.2-contributor'`,
// so on a box with no META key the auto-switch had nowhere to go: attaching an
// image hit the "no image-capable model is configured" refusal even after
// DeepSeek gained a vision model that box could run on the key it already had.
//
// The rule under test: the target FOLLOWS the server's `vision_model` — the same
// contract `default_model` already has — and nothing in the frontend names the
// vision model as a general default, so the two can no longer drift apart.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');
const CHAT_HTML = readFileSync(resolve(APP_ROOT, 'frontend', 'chat.html'), 'utf8');

const MUSE = 'muse-spark-1.2-contributor';
const DS_VISION = 'deepseek-v4-flash-vision-exp';

/** The auto-switch block, sliced out so assertions can't match elsewhere. */
const autoSwitchBlock = INDEX_JS.slice(
  INDEX_JS.indexOf('--- Auto-switch model when an image is attached'),
  INDEX_JS.indexOf('--- end auto-switch ---'),
);

test('the auto-switch block was actually found', () => {
  // Guard the slice itself — a rename would make every assertion below vacuous.
  assert.ok(autoSwitchBlock.length > 200, 'auto-switch block not found in index.js');
});

test('the switch target comes from the backend, not a constant', () => {
  assert.match(INDEX_JS, /this\.visionModel = data\.vision_model/);
  assert.match(autoSwitchBlock, /this\.visionModel/);
  assert.doesNotMatch(
    autoSwitchBlock,
    /['"]muse-spark-1\.2-contributor['"]/,
    'the auto-switch must not name a model id — it is box-dependent',
  );
});

test('an empty vision_model is preserved, not replaced by the legacy default', () => {
  // `??`, never `||`: "" is the backend saying this box has NO vision model, and
  // `||` would quietly hand it Muse, putting an image in front of a model the
  // box has no key for (401 on every send).
  assert.match(INDEX_JS, /data\.vision_model \?\? LEGACY_IMAGE_MODEL/);
  assert.doesNotMatch(INDEX_JS, /data\.vision_model \|\| /);
});

test('an older backend that omits the field keeps the pre-0.7.5 behaviour', () => {
  assert.match(INDEX_JS, /LEGACY_IMAGE_MODEL = 'muse-spark-1\.2-contributor'/);
  // ...and it is only ever a skew fallback, never the general default: the two
  // uses are the fetch fallback and the send-path guard.
  assert.equal(
    (INDEX_JS.match(/LEGACY_IMAGE_MODEL/g) || []).length,
    3,
    'declaration + fetch fallback + send-path guard',
  );
});

test('the switch notice names the model the user is actually moved to', () => {
  // Was a hardcoded IMAGE_MODEL_LABEL string, which would now lie on a DeepSeek
  // box. modelLabel() reads the label off the dropdown option instead.
  assert.doesNotMatch(INDEX_JS, /IMAGE_MODEL_LABEL/);
  assert.match(autoSwitchBlock, /this\.modelLabel\(imageModel\)/);
});

test('the vision model is offered in the dropdown so the switch has an option', () => {
  // setModel() drives the <select>; a target with no <option> silently no-ops.
  const block = CHAT_HTML.split('data-llamabot="model-select"')[1].split('</select>')[0];
  assert.ok(block.includes(`value="${DS_VISION}"`), `${DS_VISION} missing from the dropdown`);
  assert.ok(block.includes(`value="${MUSE}"`), `${MUSE} missing from the dropdown`);
});

test('a disabled target still refuses instead of sending', () => {
  // The refusal path is what stops an image reaching a text-only model and
  // 400ing mid-stream as a stack trace in the user's face.
  assert.match(autoSwitchBlock, /imageModelAvailable/);
  assert.match(autoSwitchBlock, /this\.refuseImageAttachment\(\)/);
});
