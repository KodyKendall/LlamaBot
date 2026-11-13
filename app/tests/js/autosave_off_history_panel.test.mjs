// LP-49: with auto-save turned off, the History panel stays calm.
//
// A developer who turns auto-save off will have changes outside a save point on
// purpose. The panel says so plainly — "Auto-save is off · last saved 2 min ago" —
// and never shows the LP-46 "not saved" dot on the History button. Manual Save stays.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import { describeSaveStatus, AUTOSAVE_OFF_TEXT, NOT_SAVED_TEXT } from '../../frontend/chat/checkpoints/saveStatus.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const MANAGER = readFileSync(resolve(APP, 'frontend', 'chat', 'checkpoints', 'CheckpointManager.js'), 'utf8');

const NOW = new Date('2026-11-10T14:34:05Z');
const status = (over = {}) => ({
  clean: true, last_saved_at: '2026-11-10T14:32:05Z', turn_in_progress: false,
  changed_files: 0, last_save_error: null, autosave_enabled: true, ...over,
});

test('off with changes: calm wording, last saved time, no dot, Save still offered', () => {
  const s = describeSaveStatus(status({ autosave_enabled: false, clean: false, changed_files: 3 }), NOW);
  assert.equal(s.state, 'off');
  assert.equal(s.text + s.time, 'Auto-save is off · last saved 2 min ago');
  assert.equal(AUTOSAVE_OFF_TEXT, 'Auto-save is off');
  assert.equal(s.showDot, false);
  assert.equal(s.showSaveNow, true);
  assert.equal(s.autosaveOff, true);
});

test('off while Leo works: never claims it is about to save', () => {
  const s = describeSaveStatus(status({ autosave_enabled: false, clean: false, turn_in_progress: true }), NOW);
  assert.equal(s.state, 'off');
  assert.doesNotMatch(s.text, /Saving/);
  assert.equal(s.showDot, false);
});

test('off and clean: still says auto-save is off, nothing to save', () => {
  const s = describeSaveStatus(status({ autosave_enabled: false }), NOW);
  assert.equal(s.text + s.time, 'Auto-save is off · last saved 2 min ago');
  assert.equal(s.showSaveNow, false);
});

test('off with no save yet: no dangling separator', () => {
  const s = describeSaveStatus(status({ autosave_enabled: false, last_saved_at: null }), NOW);
  assert.equal(s.text + s.time, 'Auto-save is off');
});

test('on, and an older backend that does not send the field: LP-46 behaviour unchanged', () => {
  for (const over of [{}, { autosave_enabled: undefined }]) {
    const s = describeSaveStatus(status({ ...over, clean: false }), NOW);
    assert.equal(s.state, 'not-saved');
    assert.equal(s.text, NOT_SAVED_TEXT);
    assert.equal(s.showDot, true);
    assert.equal(s.autosaveOff, false);
  }
});

test('the panel links to the setting, shown only while auto-save is off', () => {
  assert.match(MANAGER, /class="save-status-autosave-link[^"]*hidden"[^>]*href="\/settings#autosave"/);
  assert.match(MANAGER, /\.save-status-autosave-link'\)\?\.classList\.toggle\('hidden', !view\.autosaveOff\)/);
});
