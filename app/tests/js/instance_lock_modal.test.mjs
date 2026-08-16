// Behavior test for the sleep-lock IIFE in chat.html.
//
// The lock is only worth shipping if three things hold at runtime, and none of
// them are visible to a string-matching test:
//   1. an injected `locked` state paints the modal WITHOUT a network round-trip
//      (otherwise a locked instance shows a usable UI until the first poll);
//   2. a lock that arrives mid-session (poll or WebSocket frame) disables the
//      composer, and unlocking hands it back;
//   3. Escape does not close it — it is a lock, not a notice.
//
// Same approach as update_button_ordering.test.mjs: run the REAL source from
// chat.html against a minimal DOM stub, so the test breaks if the script does.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const CHAT_HTML = resolve(APP_ROOT, 'frontend', 'chat.html');

/** Pull the self-contained sleep-lock IIFE out of chat.html. */
function extractLockIife() {
  const html = readFileSync(CHAT_HTML, 'utf8');
  const anchor = html.indexOf('===================== Instance sleep lock =====================', html.indexOf('<script>'));
  assert.ok(anchor > 0, 'could not locate the sleep-lock script in chat.html');
  const start = html.indexOf('(function () {', anchor);
  assert.ok(start > 0, 'could not locate the sleep-lock IIFE');
  const end = html.indexOf('})();', start);
  assert.ok(end > start, 'could not locate the end of the sleep-lock IIFE');
  return html.slice(start, end + '})();'.length);
}

class FakeClassList {
  constructor(initial = []) { this._set = new Set(initial); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
  toggle(n, force) {
    const on = force === undefined ? !this._set.has(n) : !!force;
    if (on) this._set.add(n); else this._set.delete(n);
    return on;
  }
}

class FakeElement {
  constructor(name, classes = []) {
    this.name = name;
    this.classList = new FakeClassList(classes);
    this.textContent = '';
    this.href = '';
    this.disabled = false;
    this.blurred = false;
  }
  blur() { this.blurred = true; }
}

function makeHarness({ readyState = 'complete' } = {}) {
  const elements = new Map([
    ['lock-modal', new FakeElement('lock-modal', ['hidden'])],
    ['lock-modal-title', new FakeElement('lock-modal-title')],
    ['lock-modal-body', new FakeElement('lock-modal-body')],
    ['lock-modal-cta', new FakeElement('lock-modal-cta')],
    ['message-input', new FakeElement('message-input')],
    ['send-button', new FakeElement('send-button')],
  ]);
  const keyHandlers = [];

  const document = {
    readyState,
    body: new FakeElement('body'),
    querySelector(selector) {
      const m = /\[data-llamabot="([^"]+)"\]/.exec(selector);
      return (m && elements.get(m[1])) || null;
    },
    addEventListener(type, fn) { if (type === 'keydown') keyHandlers.push(fn); },
  };

  const window = { LLAMABOT_INSTANCE_LOCK: undefined };
  return { elements, keyHandlers, document, window };
}

function run(h, { fetchImpl = () => new Promise(() => {}) } = {}) {
  const intervals = [];
  const fn = new Function(
    'document', 'window', 'fetch', 'setInterval', 'console',
    extractLockIife(),
  );
  fn(h.document, h.window, fetchImpl, (cb) => { intervals.push(cb); return 1; }, { log() {}, warn() {} });
  return intervals;
}

test('an injected lock paints the modal with no network round-trip', () => {
  const h = makeHarness();
  h.window.LLAMABOT_INSTANCE_LOCK = {
    locked: true,
    title: 'Your free Leo is about to sleep',
    body: 'We are backing up your Leo so you don\'t lose your work.',
    upgrade_url: 'https://llamapress.ai/pricing',
  };

  // fetch throws: proves the first paint does NOT depend on the poll.
  run(h, { fetchImpl: () => { throw new Error('poll must not be needed for first paint'); } });

  assert.equal(h.elements.get('lock-modal').classList.contains('hidden'), false);
  assert.equal(h.elements.get('lock-modal-title').textContent, 'Your free Leo is about to sleep');
  assert.equal(h.elements.get('lock-modal-cta').href, 'https://llamapress.ai/pricing');
  assert.equal(h.document.body.classList.contains('instance-locked'), true);
});

test('an unlocked instance shows nothing and leaves the composer alone', () => {
  const h = makeHarness();
  h.window.LLAMABOT_INSTANCE_LOCK = { locked: false };
  run(h);

  assert.equal(h.elements.get('lock-modal').classList.contains('hidden'), true);
  assert.equal(h.elements.get('message-input').disabled, false);
  assert.equal(h.elements.get('send-button').disabled, false);
});

test('a lock arriving mid-session disables the composer, and unlocking restores it', () => {
  const h = makeHarness();
  h.window.LLAMABOT_INSTANCE_LOCK = { locked: false };
  run(h);

  // This is the hook the poll and the WebSocket `instance_locked` frame share.
  h.window.__llamabotApplyInstanceLock({ locked: true, title: 'Sleeping', body: 'Upgrade' });

  assert.equal(h.elements.get('lock-modal').classList.contains('hidden'), false);
  assert.equal(h.elements.get('message-input').disabled, true);
  assert.equal(h.elements.get('message-input').blurred, true, 'a focused textarea must lose focus');
  assert.equal(h.elements.get('send-button').disabled, true);

  h.window.__llamabotApplyInstanceLock({ locked: false });

  assert.equal(h.elements.get('lock-modal').classList.contains('hidden'), true);
  assert.equal(h.elements.get('message-input').disabled, false, 'an upgrade must not require a reload');
  assert.equal(h.document.body.classList.contains('instance-locked'), false);
});

test('Escape does not close the lock', () => {
  const h = makeHarness();
  h.window.LLAMABOT_INSTANCE_LOCK = { locked: true };
  run(h);

  assert.equal(h.keyHandlers.length, 1, 'the IIFE should register one keydown handler');
  let prevented = false;
  h.keyHandlers[0]({ key: 'Escape', preventDefault() { prevented = true; }, stopPropagation() {} });

  assert.equal(prevented, true, 'Escape must be swallowed while locked');
  assert.equal(h.elements.get('lock-modal').classList.contains('hidden'), false, 'the modal must stay up');
});

test('the poll applies a lock that lands while the tab is open', async () => {
  const h = makeHarness();
  h.window.LLAMABOT_INSTANCE_LOCK = { locked: false };

  let payload = { locked: false };
  const intervals = run(h, {
    fetchImpl: async (url) => {
      assert.equal(url, '/api/instance-lock');
      return { ok: true, json: async () => payload };
    },
  });

  assert.equal(intervals.length, 1, 'the IIFE should register exactly one poll timer');

  payload = { locked: true, title: 'Sleeping now', body: 'Upgrade', upgrade_url: 'https://llamapress.ai/pricing' };
  await intervals[0]();

  assert.equal(h.elements.get('lock-modal').classList.contains('hidden'), false);
  assert.equal(h.elements.get('lock-modal-title').textContent, 'Sleeping now');
});

test('a failed poll keeps the last known state instead of unlocking', async () => {
  const h = makeHarness();
  h.window.LLAMABOT_INSTANCE_LOCK = { locked: true };

  const intervals = run(h, { fetchImpl: async () => { throw new Error('offline'); } });
  await intervals[0]();

  assert.equal(
    h.elements.get('lock-modal').classList.contains('hidden'),
    false,
    'losing the network must not unlock the instance',
  );
});
