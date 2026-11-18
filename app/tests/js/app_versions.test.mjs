// LP-47: saved whole-app versions in the History panel.
//
// The words, the state shapes and the requests live in versionsView.js (no DOM),
// so they are tested here directly. The DOM wiring in VersionsSection.js is checked
// at the source level: accessible names, live regions, the confirm dialog, and that
// nothing anywhere offers a download.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  describeVersions, errorView, formatSize, statusLabel, restoreConfirmText, launchConfirmText,
  confirmMatches, restoreFinished, makeVersionsApi, SAVING_LABEL, SAVED_LABEL,
} from '../../frontend/chat/checkpoints/versionsView.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (...p) => readFileSync(resolve(APP, ...p), 'utf8');
const SECTION = read('frontend', 'chat', 'checkpoints', 'VersionsSection.js');
const VIEW = read('frontend', 'chat', 'checkpoints', 'versionsView.js');
const MANAGER = read('frontend', 'chat', 'checkpoints', 'CheckpointManager.js');

const SAVED = {
  id: 'v_1', name: 'before invoices', created_at: '2026-11-10T14:00:00Z',
  size_bytes: 52428800, status: 'saved', kind: 'user',
};

// ------------------------------------------------------------------ render states

test('empty list: section shows, Save version offered, empty note', () => {
  const v = describeVersions({ enabled: true, versions: [] });
  assert.equal(v.hidden, false);
  assert.equal(v.canSave, true);
  assert.equal(v.empty, true);
});

test('Saving then Saved, with date and size once saved', () => {
  const saving = describeVersions({ versions: [{ ...SAVED, status: 'saving', size_bytes: null }] });
  assert.equal(saving.items[0].statusLabel, SAVING_LABEL);
  assert.equal(saving.items[0].ready, false);
  assert.equal(saving.anySaving, true);

  const saved = describeVersions({ versions: [SAVED] }, 'en-US');
  const item = saved.items[0];
  assert.equal(item.statusLabel, SAVED_LABEL);
  assert.equal(item.ready, true);
  assert.equal(item.size, '50 MB');
  assert.match(item.date, /Nov 10, 2026/);
  assert.equal(saved.anySaving, false);
});

test('safety versions are marked', () => {
  const v = describeVersions({ versions: [{ ...SAVED, kind: 'safety' }] });
  assert.equal(v.items[0].safety, true);
});

test('feature off with a message: shown verbatim, nothing to click', () => {
  const v = describeVersions({ enabled: false, message: 'Saved versions are on paid plans.', upgrade_url: 'https://llamapress.ai/pricing' });
  assert.equal(v.hidden, false);
  assert.equal(v.canSave, false);
  assert.equal(v.message, 'Saved versions are on paid plans.');
  assert.equal(v.upgradeUrl, 'https://llamapress.ai/pricing');
});

test('feature off with nothing to say: the section is hidden (the rollback switch)', () => {
  assert.equal(describeVersions({ enabled: false }).hidden, true);
});

test('plan says no restore: no Restore button offered', () => {
  const v = describeVersions({ versions: [SAVED], can_restore: false });
  assert.equal(v.canRestore, false);
  assert.equal(v.canLaunch, true);
});

test('plan limit reached: the server message and pricing link, verbatim', () => {
  const e = errorView({ error: "You've used all 1 saved versions on the Free plan.", upgrade_url: 'https://llamapress.ai/pricing' });
  assert.equal(e.message, "You've used all 1 saved versions on the Free plan.");
  assert.equal(e.upgradeUrl, 'https://llamapress.ai/pricing');
  assert.match(errorView({}).message, /try again/);
});

test('a restore in progress is picked up from the list', () => {
  const v = describeVersions({ versions: [SAVED], restore: { status: 'restoring', version_id: 'v_1' } });
  assert.equal(v.restoring, true);
  assert.equal(v.restoringVersionId, 'v_1');
});

test('statusLabel and formatSize', () => {
  assert.equal(statusLabel('saved'), 'Saved');
  assert.equal(statusLabel('failed'), "Couldn't save");
  assert.equal(formatSize(1536), '1.5 KB');
  assert.equal(formatSize(null), '');
});

// ------------------------------------------------------------------ plain-language confirms

test('restore confirm says when, that the current app is saved first, and how long', () => {
  const text = restoreConfirmText({ name: 'v1', date: 'Nov 10, 2026, 2:00 PM' });
  assert.match(text, /^Your app will go back to Nov 10, 2026, 2:00 PM/);
  assert.match(text, /Your current app is saved first\./);
  assert.match(text, /Takes about 2 minutes\./);
});

test('start-new-Leo confirm says the current app stays as it is', () => {
  assert.match(launchConfirmText({ name: 'v1' }), /Your current app stays exactly as it is\./);
});

test('typed confirmation must match the version name', () => {
  assert.equal(confirmMatches('before invoices', 'before invoices'), true);
  assert.equal(confirmMatches('  Before Invoices ', 'before invoices'), true);
  assert.equal(confirmMatches('before', 'before invoices'), false);
  assert.equal(confirmMatches('', ''), false);
});

// ------------------------------------------------------------------ the requests

function fakeFetch(reply = { ok: true, status: 200, body: {} }) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, method: init.method, body: init.body ? JSON.parse(init.body) : undefined });
    return { ok: reply.ok, status: reply.status, json: async () => reply.body };
  };
  return { fn, calls };
}

test('each button hits the box proxy with the right payload', async () => {
  const { fn, calls } = fakeFetch();
  const api = makeVersionsApi(fn);
  await api.list();
  await api.save('before invoices');
  await api.restore('v_1', 'before invoices');
  await api.launch('v_1');
  await api.remove('v_1');
  assert.deepEqual(calls, [
    { url: '/api/versions', method: 'GET', body: undefined },
    { url: '/api/versions', method: 'POST', body: { name: 'before invoices' } },
    { url: '/api/versions/v_1/restore', method: 'POST', body: { confirm: 'before invoices' } },
    { url: '/api/versions/v_1/launch', method: 'POST', body: {} },
    { url: '/api/versions/v_1', method: 'DELETE', body: undefined },
  ]);
});

test('ids are encoded, so they cannot steer the path', async () => {
  const { fn, calls } = fakeFetch();
  await makeVersionsApi(fn).remove('../admin');
  assert.equal(calls[0].url, '/api/versions/..%2Fadmin');
});

test('server errors come back with their body, not thrown', async () => {
  const { fn } = fakeFetch({ ok: false, status: 402, body: { error: 'Limit reached.' } });
  const r = await makeVersionsApi(fn).save('x');
  assert.equal(r.ok, false);
  assert.equal(r.status, 402);
  assert.equal(r.body.error, 'Limit reached.');
});

// ------------------------------------------------------------------ restore reconnect

test('restore is done when the mothership says so, or after the box went down and came back', () => {
  assert.equal(restoreFinished({ ok: true, body: { restore: { status: 'restoring' } }, sawOutage: true }), false);
  assert.equal(restoreFinished({ ok: true, body: { restore: { status: 'idle' } } }), true);
  assert.equal(restoreFinished({ ok: true, body: {}, sawOutage: false }), false);
  assert.equal(restoreFinished({ ok: true, body: {}, sawOutage: true }), true);
  assert.equal(restoreFinished({ ok: false, body: {}, sawOutage: true }), false);
});

test('a finished restore reloads the page so chat and preview reconnect', () => {
  assert.match(SECTION, /restoreFinished\(poll\)[\s\S]{0,200}this\.reload\(\)/);
});

// ------------------------------------------------------------------ accessibility

test('status is announced in a live region, as text', () => {
  assert.match(SECTION, /class: 'versions-status', role: 'status', 'aria-live': 'polite'/);
  assert.match(SECTION, /class: 'versions-restoring hidden', role: 'status', 'aria-live': 'assertive'/);
});

test('every action button has an accessible name that contains its visible text', () => {
  assert.match(SECTION, /'aria-label': `Restore version \$\{item\.name\}`/);
  assert.match(SECTION, /'aria-label': `Start new Leo from this: \$\{item\.name\}`/);
  assert.match(SECTION, /'aria-label': `Delete version \$\{item\.name\}`/);
  assert.match(SECTION, /el\('label', \{ for: 'version-name-input' \}, 'Version name'\)/);
});

test('confirm dialog: modal role, labelled, Escape cancels, Tab is trapped, focus goes back', () => {
  assert.match(SECTION, /class: 'version-confirm-modal', role: 'dialog', 'aria-modal': 'true'/);
  assert.match(SECTION, /'aria-labelledby': 'version-confirm-title'/);
  assert.match(SECTION, /event\.key === 'Escape'[\s\S]{0,120}close\(false\)/);
  assert.match(SECTION, /event\.key === 'Tab'/);
  assert.match(SECTION, /opener\.isConnected \? opener : this\.saveButton\)\?\.focus\(\)/);
});

test('server text is never put into the page as HTML', () => {
  assert.doesNotMatch(SECTION, /innerHTML/);
});

// ------------------------------------------------------------------ placement + no download

test('Versions sits at the top of the History panel, above the Git save status', () => {
  const mount = MANAGER.indexOf('class="versions-mount"');
  const save = MANAGER.indexOf('data-llamabot="save-status"');
  assert.ok(mount > 0 && mount < save);
  assert.match(MANAGER, /this\.versions\.refresh\(\)/);
});

test('nothing offers a download or export of a version', () => {
  const code = (src) => src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  for (const src of [SECTION, VIEW]) {
    assert.doesNotMatch(code(src), /download|export (?:your|this|the|a)|\.tar\b|\bs3\b|amazonaws|presigned/i);
  }
});
