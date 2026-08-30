// A model swap the user did not ask for must be visible, and must not erase
// what they picked.
//
// Two halves of the same 2026-08-16 report ("I have Nemotron selected and it's
// just defaulting to Muse", "it's also not remembering the last model I used"):
//
//   1. The backend substitutes a policy-disabled model inside get_llm. The
//      dropdown went on showing the user's choice while every turn ran on the
//      box default — the only trace was a container-log WARNING. Fixed by the
//      `model_substituted` frame, rendered as a banner above the composer.
//   2. When /api/available-models reports the saved model unavailable, the
//      frontend repairs the SELECTION — but it also overwrote the llmModel
//      cookie, permanently destroying the user's pick. A model that is
//      unavailable today (key not in .env yet, missing from instance.json's
//      enabled_models) must be returned to once it works again.
//
// Source-level assertions on purpose: index.js pulls in the whole app, so the
// rules are pinned where they live (same approach as vision_model_unavailable).

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (...parts) => readFileSync(resolve(APP_ROOT, ...parts), 'utf8');

const INDEX_JS = read('frontend', 'chat', 'index.js');
const CHAT_HTML = read('frontend', 'chat.html');
const MESSAGE_HANDLER = read('frontend', 'chat', 'websocket', 'MessageHandler.js');
const STYLE_CSS = read('frontend', 'style.css');

/** The body of fetchAvailableModels' stale-selection repair. */
function repairBranch() {
  const start = INDEX_JS.indexOf('if (needsNewSelection) {');
  assert.notEqual(start, -1, 'the stale-selection repair branch moved or was renamed');
  return INDEX_JS.slice(start, INDEX_JS.indexOf('// Update the dropdown label display', start));
}

test('the swap frame has its own branch, above the transcript fallback', () => {
  // Without a branch it reaches handleGenericMessage and gets RENDERED INTO THE
  // CHAT as a message from Leo, which is worse than staying silent.
  assert.match(MESSAGE_HANDLER, /data\.type === 'model_substituted'/);
  const branchAt = MESSAGE_HANDLER.indexOf("data.type === 'model_substituted'");
  const fallbackAt = MESSAGE_HANDLER.indexOf('this.handleGenericMessage(data)');
  assert.ok(branchAt < fallbackAt, 'the frame must be handled before the generic fallback');
  assert.match(MESSAGE_HANDLER, /showModelSubstitutionNotice\(data\.requested, data\.effective, data\.reason\)/);
});

test('the notice renders above the composer, not as a corner toast', () => {
  // House preference, already stated for the image banner in index.js: a notice
  // about the composer belongs next to the composer.
  assert.match(CHAT_HTML, /data-llamabot="model-switch-banner"/);
  assert.match(CHAT_HTML, /data-llamabot="model-switch-text"/);
  const bannerAt = CHAT_HTML.indexOf('data-llamabot="model-switch-banner"');
  const inputAt = CHAT_HTML.indexOf('data-llamabot="message-input"');
  assert.ok(bannerAt < inputAt, 'the banner must sit above the message input');
  assert.match(INDEX_JS, /showModelSubstitutionNotice\(requested, effective, reason\)/);
});

test('the notice names both models, and says which one is answering', () => {
  const start = INDEX_JS.indexOf('showModelSubstitutionNotice(requested, effective, reason)');
  const body = INDEX_JS.slice(start, start + 1200);
  assert.match(body, /modelLabel\(requested\)/);
  assert.match(body, /modelLabel\(effective\)/);
});

test('the notice clears itself, and a second one restarts the clock', () => {
  const start = INDEX_JS.indexOf('showModelSubstitutionNotice(requested, effective, reason)');
  const body = INDEX_JS.slice(start, start + 1200);
  assert.match(body, /clearTimeout\(this\.modelSwitchNoticeTimer\)/,
    'a repeat notice must not be hidden early by the previous timer');
  assert.match(body, /setTimeout\(/);
});

test('the notice is styled apart from the informational image banner', () => {
  // Same shape, different colour: this one reports that something the user
  // asked for is NOT happening.
  assert.match(STYLE_CSS, /\.image-switch-banner\.model-switch-banner\s*\{/);
});

test('a model that is unavailable today does not erase the saved choice', () => {
  const branch = repairBranch();
  assert.doesNotMatch(branch, /setCookie\('llmModel'/,
    "the repair must not overwrite the user's pick — that is the "
    + '"it doesn\'t remember my model" bug');
  assert.match(branch, /this\.elements\.modelSelect\.value = firstAvailable\.value/,
    'the in-UI selection still has to move to something runnable');
});

test('a frontend-side swap raises the same notice as a backend one', () => {
  assert.match(repairBranch(), /showModelSubstitutionNotice\(/);
});

test('a manual pick is still persisted', () => {
  // Guard the fix from over-correcting into "we never save the model".
  assert.match(INDEX_JS, /setCookie\('llmModel', e\.target\.value, this\.config\.cookieExpiryDays\)/);
});


// --- 0.7.5: the same frame now also reports a mid-turn failure fallback -----
//
// When a provider accepts the request and then streams nothing, the resilience
// ladder finishes the step on another model (DynamicModelMiddleware rung 2). It
// reuses `model_substituted` rather than inventing a frame — but a user reads
// "isn't enabled on this instance" as a permissions problem, which is the wrong
// explanation for a provider outage. `reason` is what keeps the two apart.

test('a mid-turn fallback is worded as an outage, not as a policy block', () => {
  const start = INDEX_JS.indexOf('showModelSubstitutionNotice(requested, effective, reason)');
  const body = INDEX_JS.slice(start, start + 1600);
  assert.match(body, /reason === 'fallback'/,
    'both swaps would otherwise get the "not enabled on this instance" wording');
  assert.match(body, /stopped responding/);
  assert.match(body, /isn't enabled on this instance/,
    'the policy wording must survive — it is still the common case');
});

test('a swap with no reason still renders (older backends, frontend repair)', () => {
  // fetchAvailableModels' own repair calls this with two arguments, and a box
  // running an older image sends the frame without the field. Neither may throw
  // or render "undefined" at the user.
  const start = INDEX_JS.indexOf('showModelSubstitutionNotice(requested, effective, reason)');
  const body = INDEX_JS.slice(start, start + 1600);
  assert.match(body, /reason === 'fallback'\s*\n?\s*\?/,
    'must be a ternary defaulting to the policy wording, not an if/else on truthiness');
});
