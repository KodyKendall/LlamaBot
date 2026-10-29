// LP-21 (Michaela, 2026-10-27): Muse failed mid-turn, Leo finished on some other
// model, and the banner said "stopped responding" for 12 seconds and vanished.
// She could not pick the fallback, could not see why it was needed, and lost
// track of which model was answering.
//
// Source-level assertions, same approach as model_substitution_notice.test.mjs:
// index.js pulls in the whole app, so the rules are pinned where they live.

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

function methodBody(signature, length = 2500) {
  const start = INDEX_JS.indexOf(signature);
  assert.notEqual(start, -1, `${signature} moved or was renamed`);
  return INDEX_JS.slice(start, start + length);
}

// --- FR-1: the picker -------------------------------------------------------

test('the model picker has a Backup dropdown defaulting to Automatic', () => {
  const container = CHAT_HTML.indexOf('data-llamabot="model-selector-container"');
  const picker = CHAT_HTML.indexOf('data-llamabot="backup-model-select"');
  assert.ok(picker > container, 'the backup picker belongs inside the model picker');
  assert.match(CHAT_HTML, /<option value="" selected>Automatic<\/option>/);
});

test('backup options are only models this box can actually run', () => {
  const body = methodBody('populateBackupModelOptions(models)');
  assert.match(body, /\.filter\(m => m\.available\)/,
    'a keyless or disabled backup is a second failure, not a fallback');
});

test('the backup pick is remembered and sent with every message', () => {
  assert.match(INDEX_JS, /setCookie\('backupModel', e\.target\.value/);
  assert.match(INDEX_JS, /getCookie\('backupModel'\)/);
  assert.match(INDEX_JS, /backup_model: this\.elements\.backupModelSelect\?\.value \|\| undefined/);
});

// --- FR-3: the banner says why, and stays --------------------------------------

test('the fallback frame carries its failure reason to the banner', () => {
  assert.match(MESSAGE_HANDLER,
    /showModelSubstitutionNotice\(data\.requested, data\.effective, data\.reason, data\.failure\)/);
});

test('every failure code the backend sends has its own plain-language reason', () => {
  const body = methodBody('modelFailureReason(requested, failure)');
  for (const code of ['model_gone', 'usage_limit', 'auth', 'rate_limited', 'bad_request', 'stall']) {
    assert.match(body, new RegExp(`case '${code}'`), `no wording for ${code}`);
  }
  assert.match(body, /resets_at/, 'a usage limit must say when it resets');
});

test('the banner no longer hides itself on a timer', () => {
  const body = methodBody('showModelSubstitutionNotice(requested, effective, reason, failure)', 1800);
  assert.doesNotMatch(body, /setTimeout\(/, 'Michaela lost the banner after 12 seconds');
  assert.doesNotMatch(INDEX_JS, /modelSwitchNoticeTimer/);
});

test('a clean turn clears the banner, a turn that fell back again keeps it', () => {
  assert.match(MESSAGE_HANDLER, /clearModelNoticeIfRecovered\(\)/);
  const body = methodBody('clearModelNoticeIfRecovered()', 600);
  assert.match(body, /if \(this\.modelNoticeThisTurn\) return;/);
  assert.match(INDEX_JS, /this\.modelNoticeThisTurn = false;/,
    'sending a message has to start the new turn with a clean slate');
});

// --- FR-4: which model is answering -------------------------------------------

test('the toolbar says a backup model is answering', () => {
  const body = methodBody('showModelSubstitutionNotice(requested, effective, reason, failure)', 1800);
  assert.match(body, /this\.showBackupInUse\(effective\)/);
  const show = methodBody('showBackupInUse(model)', 600);
  assert.match(show, /\(backup\)/);
});
