// Tests for the Messages tab's red unread badge, and for the Settings-driven
// tab-visibility gate that can switch the tab off entirely.
//
// The chat UI and the Rails app are different origins, so the chat window can't
// count unread messages itself. The Rails messages iframe posts the count up
// (see the gem's app/views/llama_bot_rails/shared/_parent_unread_bridge.html.erb)
// and IframeManager only renders it — which means the render path has to be
// defensive about whatever arrives on that channel.
//
// Stub DOM in the same style as iframe_session_restore.test.mjs (no jsdom).

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
    this.textContent = '';
    this.attributes = {};
    this.handlers = {};
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  click() { (this.handlers.click || []).forEach((fn) => fn({ target: { closest: () => null } })); }
}

function makeStorage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { map.set(k, String(v)); },
  };
}

function makeEnv({ storage = makeStorage(), role = 'engineer', visibleTabs = undefined } = {}) {
  const els = {
    'live-site-frame': new FakeElement({ classes: ['content-iframe', 'active'] }),
    'vscode-frame': new FakeElement({ classes: ['content-iframe'] }),
    'inbox-frame': new FakeElement({ classes: ['content-iframe'] }),
    'activity-frame': new FakeElement({ classes: ['content-iframe'] }),
    'messages-unread-badge': new FakeElement({ classes: ['hidden'] }),
    'url-input': new FakeElement(),
    'url-dropdown': new FakeElement(),
  };

  const tabs = [
    new FakeElement({ classes: ['tab', 'active'], dataset: { target: 'liveSiteFrame' } }),
    new FakeElement({ classes: ['tab'], dataset: { target: 'inboxFrame' } }),
    new FakeElement({ classes: ['tab'], dataset: { target: 'activityFrame', engineerOnly: 'true' } }),
  ];
  const iframes = ['live-site-frame', 'inbox-frame', 'activity-frame']
    .map((k) => els[k]);

  const container = {
    querySelector(sel) {
      const m = /\[data-llamabot="([^"]+)"\]/.exec(sel);
      return (m && els[m[1]]) || null;
    },
    querySelectorAll(sel) {
      if (sel === '.tab') return tabs;
      if (sel === '.content-iframe') return iframes;
      if (sel === '.tab-external-link') return [];
      return [];
    },
  };

  const messageListeners = [];
  globalThis.window = {
    location: { protocol: 'https:', host: 'box.llamapress.ai', pathname: '/chat', search: '', hash: '' },
    history: { replaceState() {} },
    localStorage: storage,
    LLAMABOT_USER_ROLE: role,
    LLAMABOT_VISIBLE_TABS: visibleTabs,
    addEventListener(type, fn) { if (type === 'message') messageListeners.push(fn); },
  };
  globalThis.document = { addEventListener() {}, querySelector: () => null };

  return { container, els, tabs, iframes, storage, messageListeners };
}

const RAILS = 'https://rails-box.llamapress.ai';
const TAB_KEY = `llamabot:lastTab:${RAILS}`;

const { IframeManager } = await import('../../frontend/chat/ui/IframeManager.js');

/** Deliver a message event to every listener the manager registered. */
function post(env, data) {
  env.messageListeners.forEach((fn) => fn({ data }));
}

function badge(env) { return env.els['messages-unread-badge']; }

// ---------------------------------------------------------------------------
// iframe wiring
// ---------------------------------------------------------------------------

test('the inbox and activity frames point at the Rails engine', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  // /inbox is an entry point that redirects to the first page this user may
  // open; Tickets is engineers-only, so the tab cannot hardcode a page.
  assert.equal(env.els['inbox-frame'].src, `${RAILS}/llama_bot/inbox`);
  assert.equal(env.els['activity-frame'].src, `${RAILS}/llama_bot/activity`);
});

test('the inbox frame loads eagerly, so its unread poll runs before the tab is opened', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  // Not the active tab, but still sourced — the badge depends on this.
  assert.ok(!env.els['inbox-frame'].classList.contains('active'));
  assert.notEqual(env.els['inbox-frame'].src, '');
});

// ---------------------------------------------------------------------------
// badge rendering
// ---------------------------------------------------------------------------

test('an unread count from the messages iframe shows the badge', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 3 });

  assert.equal(badge(env).textContent, '3');
  assert.ok(!badge(env).classList.contains('hidden'));
  assert.equal(badge(env).attributes['aria-label'], '3 unread messages');
});

test('a count of one is not pluralized', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 1 });

  assert.equal(badge(env).attributes['aria-label'], '1 unread message');
});

test('dropping to zero hides the badge again', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 4 });
  assert.ok(!badge(env).classList.contains('hidden'));

  // What the bridge posts after the user opens the conversation and it is read.
  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 0 });

  assert.ok(badge(env).classList.contains('hidden'));
  assert.equal(badge(env).attributes['aria-label'], undefined);
});

test('a large count is capped so the badge cannot stretch the tab strip', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 1200 });

  assert.equal(badge(env).textContent, '99+');
  assert.equal(badge(env).attributes['aria-label'], '1200 unread messages',
    'the screen-reader label should still carry the real number');
});

test('garbage on the channel is treated as zero, not rendered', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 5 });
  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: 'lots' });

  assert.ok(badge(env).classList.contains('hidden'));
});

test('a negative count cannot show a badge', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  post(env, { source: 'llamabot-notifications', type: 'unread-count', unreadMessages: -1 });

  assert.ok(badge(env).classList.contains('hidden'));
});

test('messages from other senders are ignored', () => {
  const env = makeEnv();
  new IframeManager(env.container);

  // The Rails navigation channel shares this window's message bus.
  post(env, { source: 'llamapress-navigation', type: 'page-loaded', path: '/posts' });
  post(env, { source: 'somewhere-else', type: 'unread-count', unreadMessages: 9 });
  post(env, { source: 'llamabot-notifications', type: 'connected', unreadMessages: 9 });

  assert.ok(badge(env).classList.contains('hidden'));
});

// ---------------------------------------------------------------------------
// tab visibility setting
// ---------------------------------------------------------------------------

test('a tab switched off in Settings is not restored as the active tab', () => {
  const env = makeEnv({
    storage: makeStorage({ [TAB_KEY]: 'inboxFrame' }),
    visibleTabs: ['liveSiteFrame', 'activityFrame'],
  });
  new IframeManager(env.container).initTabSwitching();

  const inboxTab = env.tabs.find((t) => t.dataset.target === 'inboxFrame');
  assert.ok(!inboxTab.classList.contains('active'));
  assert.ok(env.tabs[0].classList.contains('active'), 'the App tab should stay active');
});

test('the App tab is restored even when it is somehow missing from the setting', () => {
  const env = makeEnv({
    storage: makeStorage({ [TAB_KEY]: 'liveSiteFrame' }),
    visibleTabs: [],
  });
  new IframeManager(env.container).initTabSwitching();

  assert.ok(env.tabs[0].classList.contains('active'));
});

test('with no setting present every remembered tab still restores', () => {
  const env = makeEnv({ storage: makeStorage({ [TAB_KEY]: 'inboxFrame' }) });
  new IframeManager(env.container).initTabSwitching();

  const inboxTab = env.tabs.find((t) => t.dataset.target === 'inboxFrame');
  assert.ok(inboxTab.classList.contains('active'));
  assert.ok(env.els['inbox-frame'].classList.contains('active'));
});
