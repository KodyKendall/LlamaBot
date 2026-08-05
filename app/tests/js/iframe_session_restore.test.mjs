// Tests for iframe session restore: after a FULL browser refresh of chat.html the
// preview must reopen the page (and tab) the user was last on, instead of always
// snapping back to the Rails app root.
//
// The path was already tracked in memory (IframeManager.currentPath, fed by the
// Rails app's postMessage navigation events) — it just died with the page. These
// tests drive the real IframeManager against a stub DOM + stub localStorage, and
// simulate a refresh by constructing a SECOND manager over the same storage.

import assert from 'node:assert/strict';
import test from 'node:test';

// ---------------------------------------------------------------------------
// Minimal DOM / storage stubs (no jsdom dependency, matching the other js tests)
// ---------------------------------------------------------------------------

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
    this.handlers = {};
  }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  click() { (this.handlers.click || []).forEach((fn) => fn({ target: { closest: () => null } })); }
}

/** Storage stub; `broken: true` makes every call throw, like a blocked Safari. */
function makeStorage(initial = {}, { broken = false } = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem(k) { if (broken) throw new Error('storage blocked'); return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { if (broken) throw new Error('storage blocked'); map.set(k, String(v)); },
  };
}

/**
 * Build a chat.html-shaped container plus the globals IframeManager reads.
 * `storage` is shared across calls to simulate a page refresh in one browser.
 */
function makeEnv({ storage = makeStorage(), search = '', role = 'engineer' } = {}) {
  const els = {
    'live-site-frame': new FakeElement({ classes: ['content-iframe', 'active'] }),
    'vscode-frame': new FakeElement({ classes: ['content-iframe'] }),
    'tickets-frame': new FakeElement({ classes: ['content-iframe'] }),
    'feedback-frame': new FakeElement({ classes: ['content-iframe'] }),
    'url-input': new FakeElement(),
    'url-dropdown': new FakeElement(),
  };

  const tabs = [
    new FakeElement({ classes: ['tab', 'active'], dataset: { target: 'liveSiteFrame' } }),
    new FakeElement({ classes: ['tab'], dataset: { target: 'vsCodeFrame', engineerOnly: 'true' } }),
    new FakeElement({ classes: ['tab'], dataset: { target: 'ticketsFrame', engineerOnly: 'true' } }),
    new FakeElement({ classes: ['tab'], dataset: { target: 'feedbackFrame' } }),
  ];
  const iframes = ['live-site-frame', 'vscode-frame', 'tickets-frame', 'feedback-frame'].map((k) => els[k]);

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
    location: { protocol: 'https:', host: 'box.llamapress.ai', pathname: '/chat', search, hash: '' },
    history: { replaceState() {} },
    localStorage: storage,
    LLAMABOT_USER_ROLE: role,
    addEventListener(type, fn) { if (type === 'message') messageListeners.push(fn); },
  };
  globalThis.document = { addEventListener() {}, querySelector: () => null };

  return { container, els, tabs, iframes, storage, messageListeners };
}

const RAILS = 'https://rails-box.llamapress.ai';
const PATH_KEY = `llamabot:lastPath:${RAILS}`;
const TAB_KEY = `llamabot:lastTab:${RAILS}`;

// Imported after the stubs exist as a habit; the module reads `window` only at
// call time, so ordering doesn't actually matter here.
const { IframeManager } = await import('../../frontend/chat/ui/IframeManager.js');

/** Deliver a Rails page-loaded navigation message to the manager. */
function navigate(env, path) {
  env.messageListeners.forEach((fn) =>
    fn({ data: { source: 'llamapress-navigation', type: 'page-loaded', path } }));
}

// ---------------------------------------------------------------------------

test('a fresh browser still opens the app root (byte-identical to before)', () => {
  const env = makeEnv();
  new IframeManager(env.container);
  assert.equal(env.els['live-site-frame'].src, RAILS);
});

test('the last page survives a full refresh', () => {
  const storage = makeStorage();

  const first = makeEnv({ storage });
  const mgr = new IframeManager(first.container);
  navigate(first, '/posts/42/edit');
  assert.equal(mgr.currentPath, '/posts/42/edit');
  assert.equal(storage.map.get(PATH_KEY), '/posts/42/edit');

  // --- user hits browser refresh: brand new page, same localStorage ---
  const second = makeEnv({ storage });
  const restored = new IframeManager(second.container);

  assert.equal(second.els['live-site-frame'].src, `${RAILS}/posts/42/edit`);
  assert.equal(restored.currentPath, '/posts/42/edit');
  assert.equal(second.els['url-input'].value, '/posts/42/edit',
    'the URL bar should show the restored page immediately, not "/"');
});

test('navigating back to the root clears the remembered deep link', () => {
  const storage = makeStorage({ [PATH_KEY]: '/posts/42' });
  const env = makeEnv({ storage });
  const mgr = new IframeManager(env.container);
  navigate(env, '/');
  assert.equal(storage.map.get(PATH_KEY), '/');

  const after = makeEnv({ storage });
  new IframeManager(after.container);
  assert.equal(after.els['live-site-frame'].src, RAILS);
  assert.ok(mgr); // manager stays usable
});

test('navigateToPath (url bar / routes dropdown) is remembered too', () => {
  const storage = makeStorage();
  const env = makeEnv({ storage });
  const mgr = new IframeManager(env.container);
  mgr.navigateToPath('/admin/users');
  assert.equal(storage.map.get(PATH_KEY), '/admin/users');
});

test('the remembered path is threaded through the Unified Login consume redirect', () => {
  const storage = makeStorage({ [PATH_KEY]: '/dashboard' });
  const env = makeEnv({ storage, search: '?rails_token=abc123' });
  new IframeManager(env.container);

  assert.equal(
    env.els['live-site-frame'].src,
    `${RAILS}/llamapress_auth/consume?token=abc123&return_to=%2Fdashboard`,
    'return_to must carry the restored page so post-login lands there',
  );
});

test('a hostile or malformed stored path never becomes the iframe src', () => {
  for (const bad of ['//evil.com', '/\\evil.com', 'https://evil.com', 'posts/1', '/a\nb', '']) {
    const env = makeEnv({ storage: makeStorage({ [PATH_KEY]: bad }) });
    new IframeManager(env.container);
    assert.equal(env.els['live-site-frame'].src, RAILS, `stored path ${JSON.stringify(bad)} leaked through`);
  }
});

test('storage that throws does not break the preview', () => {
  const env = makeEnv({ storage: makeStorage({}, { broken: true }) });
  const mgr = new IframeManager(env.container);
  assert.equal(env.els['live-site-frame'].src, RAILS);
  navigate(env, '/posts/1'); // must not throw
  assert.equal(mgr.currentPath, '/posts/1');
});

test('the last tab is remembered and reopened', () => {
  const storage = makeStorage();

  const first = makeEnv({ storage });
  const mgr = new IframeManager(first.container);
  mgr.initTabSwitching();
  first.tabs[3].click(); // Feedback
  assert.equal(storage.map.get(TAB_KEY), 'feedbackFrame');

  const second = makeEnv({ storage });
  const restored = new IframeManager(second.container);
  restored.initTabSwitching();

  assert.equal(second.tabs[3].classList.contains('active'), true, 'Feedback tab should be active');
  assert.equal(second.tabs[0].classList.contains('active'), false, 'Your App tab should no longer be active');
  assert.equal(second.els['feedback-frame'].classList.contains('active'), true);
  assert.equal(second.els['live-site-frame'].classList.contains('active'), false);
});

// Regression: restore worked in IframeManager but was DEAD on every real box. A leftover
// line in WebSocketManager.connectWebSocket() re-pointed the same iframe at the Rails root
// — gated on https, which is every deployed box (plain-http localhost never hit it, which
// is why it survived). It ran in the same synchronous initComponents() pass, AFTER the
// manager, so the restored URL was overwritten before the browser ever loaded it. Nothing
// in the manager's own tests could see it; the bug only exists at the seam.
test('connecting the websocket does not stomp the restored iframe src', async () => {
  const storage = makeStorage({ [PATH_KEY]: '/posts/42' });
  const env = makeEnv({ storage });

  globalThis.WebSocket = class { constructor(url) { this.url = url; } send() {} close() {} };
  const { WebSocketManager } = await import('../../frontend/chat/websocket/WebSocketManager.js');

  new IframeManager(env.container);
  assert.equal(env.els['live-site-frame'].src, `${RAILS}/posts/42`);

  // Same synchronous init pass the real page runs (index.js initComponents).
  const ws = new WebSocketManager({}, {}, { liveSiteFrame: env.els['live-site-frame'] });
  ws.connectWebSocket();

  assert.equal(
    env.els['live-site-frame'].src,
    `${RAILS}/posts/42`,
    'the websocket layer must not own the iframe src — IframeManager already set it',
  );
});

test('an engineer-only tab is not restored for a "user" role', () => {
  // Storage written while the box was in engineer mode (or hand-edited).
  const storage = makeStorage({ [TAB_KEY]: 'vsCodeFrame' });
  const env = makeEnv({ storage, role: 'user' });
  const mgr = new IframeManager(env.container);
  mgr.initTabSwitching();

  assert.equal(env.tabs[0].classList.contains('active'), true, 'should stay on Your App');
  assert.equal(env.els['vscode-frame'].classList.contains('active'), false,
    'restoring a tab the role gate is about to hide would strand the user');
});
