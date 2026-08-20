// Escape dismisses the "Your App is Building!" overlay.
//
// It HIDES, it does not cancel: the agent keeps running and the run's own
// completion path is what tears the overlay down normally. So the properties
// under test are that Escape reaches removeStreamingOverlay(), that it does
// nothing at all when no overlay is on screen, and that no other key does it.
//
// Drives the real IframeManager against a stub DOM, like iframe_session_restore.

import assert from 'node:assert/strict';
import test from 'node:test';

class FakeClassList {
  constructor(initial = []) { this._set = new Set(initial); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
}

class FakeElement {
  constructor({ classes = [], dataset = {} } = {}) {
    this.classList = new FakeClassList(classes);
    this.dataset = dataset;
    this.src = '';
    this.value = '';
    this.removed = false;
  }
  addEventListener() {}
  remove() { this.removed = true; }
}

/** chat.html-shaped container plus the globals IframeManager reads. */
function makeEnv() {
  const els = {
    'live-site-frame': new FakeElement({ classes: ['content-iframe', 'active'] }),
    'url-input': new FakeElement(),
    'url-dropdown': new FakeElement(),
  };
  const container = {
    querySelector(sel) {
      const m = /\[data-llamabot="([^"]+)"\]/.exec(sel);
      return (m && els[m[1]]) || null;
    },
    querySelectorAll(sel) {
      if (sel === '.content-iframe') return [els['live-site-frame']];
      return [];
    },
  };

  const keyListeners = [];
  globalThis.window = {
    location: { protocol: 'https:', host: 'box.llamapress.ai', pathname: '/chat', search: '', hash: '' },
    history: { replaceState() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    LLAMABOT_USER_ROLE: 'engineer',
    addEventListener() {},
  };

  // The overlay is a real DOM node in the app; here it is present or absent by id.
  let overlay = null;
  globalThis.document = {
    addEventListener(type, fn) { if (type === 'keydown') keyListeners.push(fn); },
    querySelector: () => null,
    getElementById: (id) => (id === 'streamingOverlay' ? overlay : null),
  };

  return {
    container,
    showOverlay() { overlay = new FakeElement(); return overlay; },
    hideOverlay() { overlay = null; },
    press(key, init = {}) { keyListeners.forEach((fn) => fn({ key, ...init })); },
    get keyListenerCount() { return keyListeners.length; },
  };
}

const { IframeManager } = await import('../../frontend/chat/ui/IframeManager.js');

/** Build a manager whose removeStreamingOverlay is spied on. */
function makeManager(env) {
  const mgr = new IframeManager(env.container);
  const calls = [];
  mgr.removeStreamingOverlay = () => { calls.push(true); env.hideOverlay(); };
  return { mgr, calls };
}

// ---------------------------------------------------------------------------

test('Escape hides the building overlay while it is on screen', () => {
  const env = makeEnv();
  const { calls } = makeManager(env);
  env.showOverlay();

  env.press('Escape');
  assert.equal(calls.length, 1);
});

test('Escape does nothing when no overlay is on screen', () => {
  const env = makeEnv();
  const { calls } = makeManager(env);

  env.press('Escape');
  assert.equal(calls.length, 0);
});

test('a second Escape after the overlay is gone is a no-op', () => {
  const env = makeEnv();
  const { calls } = makeManager(env);
  env.showOverlay();

  env.press('Escape');
  env.press('Escape');
  assert.equal(calls.length, 1, 'the overlay is only torn down once');
});

test('other keys leave the overlay alone', () => {
  const env = makeEnv();
  const { calls } = makeManager(env);
  env.showOverlay();

  ['Enter', 'Esc', 'a', ' ', 'Tab', 'Backspace'].forEach((k) => env.press(k));
  assert.equal(calls.length, 0);
});

test('the keydown listener is registered exactly once per manager', () => {
  const env = makeEnv();
  new IframeManager(env.container);
  assert.equal(env.keyListenerCount, 1);
});

test('Escape hides the overlay no matter where focus is', () => {
  // The user is typing in the composer when they hit Escape — the handler is on
  // document, so it must still fire. Guarding on event.target would break this.
  const env = makeEnv();
  const { calls } = makeManager(env);
  env.showOverlay();

  env.press('Escape', { target: { tagName: 'TEXTAREA' } });
  assert.equal(calls.length, 1);
});

test('Escape only hides — it never cancels the run', () => {
  // Regression guard: the overlay teardown must not reach for any stop/abort
  // path. Leo keeps working; the run's own completion removes the overlay
  // normally. If someone wires cancellation in here, this fails.
  const env = makeEnv();
  const mgr = new IframeManager(env.container);
  env.showOverlay();

  const forbidden = [];
  ['stopAgent', 'cancelRun', 'abort', 'stop'].forEach((name) => {
    mgr[name] = () => forbidden.push(name);
  });
  env.press('Escape');
  assert.deepEqual(forbidden, []);
});
