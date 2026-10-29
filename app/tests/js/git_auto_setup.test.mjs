// LP-20: Push asks for GitHub sign-in itself, and retries once after it.
//
// mbc-preceptors (2026-10-27): Push failed with "terminal prompts disabled" and
// the only way out was knowing to run /gh then /gh-copy. The server now syncs
// credentials and retries on its own; when that still fails it answers
// `needs_github_auth`, and the History panel opens the sign-in modal and pushes
// once more when it completes.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

import { GitHubAuthModal } from '../../frontend/chat/checkpoints/GitHubAuthModal.js';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const MANAGER = readFileSync(
  resolve(APP_ROOT, 'frontend', 'chat', 'checkpoints', 'CheckpointManager.js'), 'utf8');

const inertTimers = { setTimeout: () => 1, clearTimeout: () => {} };
const inertTarget = { addEventListener() {}, removeEventListener() {}, visibilityState: 'visible' };

function modalUnderTest() {
  const modal = new GitHubAuthModal({
    pollerOptions: { timers: inertTimers, doc: inertTarget, win: inertTarget, fetchFn: async () => ({}) },
  });
  modal.showSuccess = () => {};
  modal.showError = () => {};
  return modal;
}

test('a completed sign-in calls back exactly once', () => {
  const modal = modalUnderTest();
  let calls = 0;
  modal.onAuthenticated = () => { calls += 1; };
  modal.startPolling('DEV', 5, 900);
  modal.poller.onResult('success', {});
  modal.poller.onResult('success', {});
  assert.equal(calls, 1, 'a second success must not push twice');
});

test('a denied or expired sign-in does not call back', () => {
  for (const status of ['denied', 'expired', 'error']) {
    const modal = modalUnderTest();
    let calls = 0;
    modal.onAuthenticated = () => { calls += 1; };
    modal.startPolling('DEV', 5, 900);
    modal.poller.onResult(status, {});
    assert.equal(calls, 0, status);
  }
});

test('start() takes the callback', () => {
  assert.match(readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'checkpoints', 'GitHubAuthModal.js'), 'utf8'),
    /async start\(\{ onAuthenticated = null \} = \{\}\)/);
});

test('Push opens sign-in on needs_github_auth, and the retry cannot loop', () => {
  assert.match(MANAGER, /data\.needs_github_auth && allowSignIn && window\.ENABLE_GITHUB_BUTTON/);
  assert.match(MANAGER, /onAuthenticated: \(\) => this\.pushToGitHub\(\{ allowSignIn: false \}\)/);
});

test('Save shows the server sentence as-is, not prefixed git text', () => {
  assert.doesNotMatch(MANAGER, /'Failed to save checkpoint: ' \+ error\.message/);
});
