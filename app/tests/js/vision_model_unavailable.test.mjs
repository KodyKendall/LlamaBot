// Acceptance 5 of the 0.7.0 model-policy change: on a box with no META key the
// only model available is text-only DeepSeek, so an attached image has nowhere
// to go. Before this, the auto-switch block simply did nothing in that case and
// the image was sent anyway — reaching a text-only model and 400ing mid-stream
// with `unknown variant image_url`, which the user sees as a stack trace.
//
// The rule under test: no usable vision model ⇒ images are refused at ATTACH
// time with an explanation, exactly like the operator's vision-off gate. This
// exercises the gate logic in isolation (index.js pulls in the whole app), so
// what is pinned is the decision — visionUsable() and how the two independent
// causes combine — not the DOM wiring.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');
const CHAT_HTML = readFileSync(resolve(APP_ROOT, 'frontend', 'chat.html'), 'utf8');

/** The gate as index.js defines it. */
const visionUsable = (visionAllowed, visionModelAvailable) =>
  visionAllowed && visionModelAvailable;

/** How fetchAvailableModels decides whether the box has a vision model. */
const resolveVisionModelAvailable = (models) =>
  (models || []).some(m => m.available && m.capabilities?.images);

test('a box with no META key has no usable vision model', () => {
  // What /api/available-models returns there: Muse enabled but keyless, and the
  // DeepSeek fallback available but text-only.
  const models = [
    { value: 'muse-spark-1.2-contributor', available: false, capabilities: { images: true } },
    { value: 'deepseek-v4-flash', available: true, capabilities: { images: false } },
  ];
  assert.equal(resolveVisionModelAvailable(models), false);
  assert.equal(visionUsable(true, false), false, 'images must be refused');
});

test('a box with a META key can see images', () => {
  const models = [
    { value: 'muse-spark-1.2-contributor', available: true, capabilities: { images: true } },
    { value: 'deepseek-v4-flash', available: true, capabilities: { images: false } },
  ];
  assert.equal(resolveVisionModelAvailable(models), true);
  assert.equal(visionUsable(true, true), true);
});

test('the operator vision switch still wins on a Muse box', () => {
  // Vision turned OFF must refuse images even where a vision model exists.
  assert.equal(visionUsable(false, true), false);
});

test('the auto-switch target is Muse, not gpt-5-nano', () => {
  assert.match(INDEX_JS, /const IMAGE_MODEL = 'muse-spark-1\.2-contributor'/);
  assert.doesNotMatch(
    INDEX_JS,
    /const IMAGE_MODEL = 'gpt-5-nano'/,
    'gpt-5-nano is out of the default enabled set — switching to it would land on a disabled model',
  );
});

test('the switch toast names the model the user is actually moved to', () => {
  assert.doesNotMatch(
    INDEX_JS,
    /I switched to GPT-5 Nano/,
    'the toast must not name a model we no longer switch to',
  );
  assert.match(INDEX_JS, /IMAGE_MODEL_LABEL/);
});

test('an unavailable image model refuses the send instead of falling through', () => {
  // The regression guarded: the `if (imageModelAvailable)` block used to have no
  // else, so an image on a no-vision box was sent to a text-only model.
  const autoSwitch = INDEX_JS.slice(
    INDEX_JS.indexOf('--- Auto-switch model when an image is attached'),
    INDEX_JS.indexOf('--- end auto-switch ---'),
  );
  assert.ok(autoSwitch.length > 0, 'auto-switch block not found — did the markers move?');
  assert.match(autoSwitch, /}\s*else\s*{/, 'the unavailable-image-model case must be handled');
  assert.match(autoSwitch, /this\.refuseImageAttachment\(\)/);
  assert.match(autoSwitch, /return;/);
});

test('the frontend no longer hardcodes the default model', () => {
  // It is box-dependent now (Muse vs DeepSeek), so it comes from the backend.
  assert.match(INDEX_JS, /data\.default_model/);
  assert.match(INDEX_JS, /this\.defaultTextModel/);
  assert.doesNotMatch(
    INDEX_JS,
    /setModel\(DEFAULT_TEXT_MODEL\)/,
    'resets/pins must use the resolved default, not the compile-time seed',
  );
});

test('the banner can explain either cause', () => {
  assert.match(CHAT_HTML, /data-llamabot="vision-reason-disabled"/);
  assert.match(CHAT_HTML, /data-llamabot="vision-reason-no-model"/);
  assert.match(INDEX_JS, /vision-reason-no-model/);
  // Both messages must still point somewhere actionable.
  const banner = CHAT_HTML.slice(
    CHAT_HTML.indexOf('data-llamabot="vision-disabled-banner"'),
    CHAT_HTML.indexOf('data-llamabot="vision-disabled-dismiss"'),
  );
  assert.equal(
    (banner.match(/support@llamapress\.ai/g) || []).length >= 2,
    true,
    'each cause needs its own support hand-off',
  );
});
