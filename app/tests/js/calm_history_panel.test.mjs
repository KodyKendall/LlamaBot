// LP-46: the History panel says "All changes saved", not "669 unsaved changes".
//
// A free user saw a growing red "unsaved" count after ~10 weeks of building —
// for work that was safe on disk — and the only button on offer was Discard.
// Leo now saves at the end of every turn, and the panel only asks the user to
// act when something really isn't in a save point.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  describeSaveStatus, relativeTime, undoChangesConfirmText,
  SAVED_TEXT, WORKING_TEXT, NOT_SAVED_TEXT,
} from '../../frontend/chat/checkpoints/saveStatus.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (...p) => readFileSync(resolve(APP, ...p), 'utf8');
const MANAGER = read('frontend', 'chat', 'checkpoints', 'CheckpointManager.js');
const CHAT_HTML = read('frontend', 'chat.html');
const HANDLER = read('frontend', 'chat', 'websocket', 'MessageHandler.js');

const NOW = new Date('2026-11-10T14:34:05Z');
const status = (over = {}) => ({
  clean: true, last_saved_at: '2026-11-10T14:32:05Z', turn_in_progress: false,
  changed_files: 0, last_save_error: null, ...over,
});

// ---------------------------------------------------------------- the three states

test('saved: green check text plus how long ago', () => {
  const s = describeSaveStatus(status(), NOW);
  assert.equal(s.state, 'saved');
  assert.equal(s.text + s.time, 'All changes saved · 2 min ago');
  assert.equal(s.showSaveNow, false);
  assert.equal(s.showDot, false);
});

test('Leo working on edited files: "Saving after Leo finishes…", no dot', () => {
  const s = describeSaveStatus(status({ clean: false, changed_files: 3, turn_in_progress: true }), NOW);
  assert.equal(s.text, WORKING_TEXT);
  assert.equal(s.showDot, false);
  assert.equal(s.showSaveNow, false);
});

test('edits outside Leo, or a failed save: calm text, Save now, neutral dot', () => {
  const s = describeSaveStatus(status({ clean: false, changed_files: 4 }), NOW);
  assert.equal(s.text, NOT_SAVED_TEXT);
  assert.equal(s.showSaveNow, true);
  assert.equal(s.showDot, true);
});

test('a failed save never reads as saved', () => {
  const s = describeSaveStatus(status({ clean: false, changed_files: 1, last_save_error: 'Leo couldn\'t save.' }), NOW);
  assert.notEqual(s.text, SAVED_TEXT);
  assert.equal(s.detail, 'Leo couldn\'t save.');
});

test('no state ever shows a count of "unsaved" anything', () => {
  for (const over of [{}, { clean: false, changed_files: 669 }, { clean: false, changed_files: 669, turn_in_progress: true }]) {
    const s = describeSaveStatus(status(over), NOW);
    assert.doesNotMatch(`${s.text}${s.time}${s.detail}`, /unsaved|669/i);
  }
  assert.doesNotMatch(MANAGER, /unsaved change/);
});

test('relative time', () => {
  assert.equal(relativeTime('2026-11-10T14:34:00Z', NOW), 'just now');
  assert.equal(relativeTime('2026-11-10T13:30:00Z', NOW), '1 hour ago');
  assert.equal(relativeTime('2026-11-08T14:00:00Z', NOW), '2 days ago');
  assert.equal(relativeTime(null, NOW), '');
});

test('undo confirm names the file count', () => {
  assert.match(undoChangesConfirmText(4), /^This will undo changes to 4 files since your last save\./);
  assert.match(undoChangesConfirmText(1), /changes to 1 file since/);
});

// ---------------------------------------------------------------- the panel and badge

test('the History button has no red badge, just a labelled neutral dot', () => {
  assert.doesNotMatch(CHAT_HTML, /class="unsaved-badge/);
  assert.match(CHAT_HTML, /class="save-pending-dot hidden"[^>]*data-llamabot="unsaved-badge"/);
  assert.match(MANAGER, /DOT_LABEL/);
});

test('the status is in a polite live region, separate from the ticking time', () => {
  assert.match(MANAGER, /class="save-status-text" role="status" aria-live="polite"/);
  assert.match(MANAGER, /class="save-status-time"/);
});

test('panel wording: Save now, Changed since last save, Undo changes, Go back, the data note', () => {
  assert.match(MANAGER, /Save now/);
  assert.match(MANAGER, /Changed since last save/);
  assert.match(MANAGER, /Undo changes since last save/);
  assert.match(MANAGER, /Go back to this version/);
  assert.match(MANAGER, /Your current version is saved first\./);
  assert.match(MANAGER, /Saves your app's code\. Your data is backed up separately by LlamaPress\./);
  assert.doesNotMatch(MANAGER, /Rollback to this commit/);
});

test('the panel reads /api/checkpoints/status and re-reads when a turn saves', () => {
  assert.match(MANAGER, /fetch\('\/api\/checkpoints\/status'/);
  assert.match(HANDLER, /data\.type === 'save_status'/);
});

// ---------------------------------------------------------------- behaviour

function cls(initial = '') {
  const set = new Set(initial.split(' ').filter(Boolean));
  return {
    add: (c) => set.add(c), remove: (c) => set.delete(c), contains: (c) => set.has(c),
    toggle: (c, on) => { (on ?? !set.has(c)) ? set.add(c) : set.delete(c); },
  };
}
function node(c = '') {
  const attrs = {};
  return {
    classList: cls(c), textContent: '', className: c,
    setAttribute: (k, v) => { attrs[k] = v; }, getAttribute: (k) => attrs[k],
    removeAttribute: (k) => { delete attrs[k]; }, attrs,
  };
}

const { CheckpointManager } = await import('../../frontend/chat/checkpoints/CheckpointManager.js');

function managerShowing(apiStatus) {
  const parts = {
    '.save-status': node(), '.save-status-text': node(), '.save-status-time': node(),
    '.save-status-detail': node('hidden'), '.btn-save-now': node('hidden'),
    '.save-status-files-toggle': node('hidden'), '.changed-files-section': node('hidden'),
    '.save-status-icon': node(),
  };
  const m = Object.create(CheckpointManager.prototype);
  m.unsavedBadge = node('hidden');
  m.checkpointButton = node();
  m.checkpointPanel = { querySelector: (s) => parts[s] };
  globalThis.fetch = async () => ({ ok: true, json: async () => apiStatus });
  return { m, parts };
}

test('refresh: saved hides the dot and Save now', async () => {
  const { m, parts } = managerShowing(status());
  await m.refreshSaveStatus();
  assert.equal(parts['.save-status-text'].textContent, SAVED_TEXT);
  assert.ok(m.unsavedBadge.classList.contains('hidden'));
  assert.ok(parts['.btn-save-now'].classList.contains('hidden'));
});

test('refresh: not saved shows the dot with its label and Save now', async () => {
  const { m, parts } = managerShowing(status({ clean: false, changed_files: 2 }));
  await m.refreshSaveStatus();
  assert.equal(parts['.save-status-text'].textContent, NOT_SAVED_TEXT);
  assert.ok(!m.unsavedBadge.classList.contains('hidden'));
  assert.equal(m.unsavedBadge.attrs['aria-label'], 'Changes not yet saved');
  assert.match(m.checkpointButton.attrs['aria-label'], /changes not yet saved/i);
  assert.ok(!parts['.btn-save-now'].classList.contains('hidden'));
  assert.ok(!parts['.save-status-files-toggle'].classList.contains('hidden'));
});

test('refresh: a clean tree hides "Changed since last save" and with it Undo changes', async () => {
  const { m, parts } = managerShowing(status());
  parts['.save-status-files-toggle'].classList.remove('hidden');
  parts['.changed-files-section'].classList.remove('hidden');
  await m.refreshSaveStatus();
  assert.ok(parts['.save-status-files-toggle'].classList.contains('hidden'));
  assert.ok(parts['.changed-files-section'].classList.contains('hidden'));
});

test('the live region is only rewritten when the state changes', async () => {
  const { m, parts } = managerShowing(status());
  await m.refreshSaveStatus();
  let writes = 0;
  const text = parts['.save-status-text'];
  let value = text.textContent;
  Object.defineProperty(text, 'textContent', { get: () => value, set: (v) => { writes += 1; value = v; } });
  await m.refreshSaveStatus();
  assert.equal(writes, 0, 're-announcing every poll would talk over the user');
});

test('Save now posts one checkpoint and refreshes', async () => {
  const m = Object.create(CheckpointManager.prototype);
  const posts = [];
  let refreshed = 0;
  m.chatApp = {};
  m.showSuccess = () => {};
  m.showError = (e) => { throw new Error(e); };
  m.fetchCheckpoints = () => {};
  m.refreshSaveStatus = async () => { refreshed += 1; };
  m.checkUncommittedChanges = () => {};
  globalThis.fetch = async (url, opts = {}) => {
    if (opts.method === 'POST') posts.push([url, JSON.parse(opts.body)]);
    return { ok: true, json: async () => ({ checkpoint_id: 'abc' }) };
  };
  await m.saveNow();
  assert.equal(posts.length, 1);
  assert.equal(posts[0][0], '/api/checkpoints');
  assert.equal(refreshed, 1);
});

test('Go back confirms that the current version is saved first', () => {
  const m = Object.create(CheckpointManager.prototype);
  let asked = '';
  m.checkpoints = [];
  m.graphData = { commits: [{ sha: 'abc', subject: 'Leo: Add a page' }] };
  m.rollback = () => {};
  globalThis.confirm = (msg) => { asked = msg; return false; };
  m.confirmRollback('abc');
  assert.match(asked, /Leo: Add a page/);
  assert.match(asked, /Your current version is saved first/);
  assert.doesNotMatch(asked, /cannot be undone/);
});
