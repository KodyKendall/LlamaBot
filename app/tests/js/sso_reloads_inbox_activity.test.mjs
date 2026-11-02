// LP-10: after the chat's Unified Login (SSO) consume, the preview's Inbox and
// Activity frames stayed on the engine sign-in page. All three frames get their
// src at the same moment, so Inbox/Activity hit Rails BEFORE the consume in the
// main preview frame had set the session cookie — and nothing ever reloaded them.
// Fix: when a rails_token was threaded, reload Inbox + Activity once the main
// preview frame finishes loading (= the consume redirect has landed).

import assert from 'node:assert/strict';
import test from 'node:test';

class FakeClassList {
  constructor(initial = []) { this._set = new Set(initial); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
}

class FakeFrame {
  constructor(classes = []) {
    this.classList = new FakeClassList(classes);
    this.dataset = {};
    this.value = '';
    this.handlers = {};
    this.srcAssignments = 0;
    this._src = '';
  }
  get src() { return this._src; }
  set src(v) { this._src = v; this.srcAssignments += 1; }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  fire(type) { (this.handlers[type] || []).slice().forEach((fn) => fn({})); }
}

function makeEnv({ search = '' } = {}) {
  const els = {
    'live-site-frame': new FakeFrame(['content-iframe', 'active']),
    'inbox-frame': new FakeFrame(['content-iframe']),
    'activity-frame': new FakeFrame(['content-iframe']),
    'url-input': new FakeFrame(),
  };
  const container = {
    querySelector(sel) {
      const m = /\[data-llamabot="([^"]+)"\]/.exec(sel);
      return (m && els[m[1]]) || null;
    },
    querySelectorAll() { return []; },
  };
  const store = new Map();
  globalThis.window = {
    location: { protocol: 'https:', host: 'box.llamapress.ai', pathname: '/chat', search, hash: '' },
    history: { replaceState() {} },
    localStorage: { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) },
    LLAMABOT_USER_ROLE: 'engineer',
    addEventListener() {},
  };
  globalThis.document = { addEventListener() {}, querySelector: () => null };
  return { container, els };
}

const { IframeManager } = await import('../../frontend/chat/ui/IframeManager.js');

test('after an SSO consume lands, Inbox and Activity reload', () => {
  const env = makeEnv({ search: '?rails_token=abc123' });
  new IframeManager(env.container);
  const inboxBefore = env.els['inbox-frame'].srcAssignments;
  const activityBefore = env.els['activity-frame'].srcAssignments;

  env.els['live-site-frame'].fire('load');

  assert.equal(env.els['inbox-frame'].srcAssignments, inboxBefore + 1, 'inbox frame should reload');
  assert.equal(env.els['activity-frame'].srcAssignments, activityBefore + 1, 'activity frame should reload');
  assert.match(env.els['inbox-frame'].src, /\/llama_bot\/inbox$/);
  assert.match(env.els['activity-frame'].src, /\/llama_bot\/activity$/);
});

test('the reload happens once, not on every later preview navigation', () => {
  const env = makeEnv({ search: '?rails_token=abc123' });
  new IframeManager(env.container);
  env.els['live-site-frame'].fire('load');
  const after = env.els['inbox-frame'].srcAssignments;
  env.els['live-site-frame'].fire('load');
  env.els['live-site-frame'].fire('load');
  assert.equal(env.els['inbox-frame'].srcAssignments, after);
});

test('no SSO token: preview loads never touch Inbox/Activity', () => {
  const env = makeEnv();
  new IframeManager(env.container);
  const before = env.els['inbox-frame'].srcAssignments;
  env.els['live-site-frame'].fire('load');
  assert.equal(env.els['inbox-frame'].srcAssignments, before);
});
