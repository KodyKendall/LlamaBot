// A new chat was missing from the history menu until a page reload (0.7.15, LP-5).
//
// The list was fetched once at page load and the hamburger re-showed that same
// list, so a chat started afterwards never appeared. A reload also always opened
// a blank chat, so a client who stepped away to read a document Leo made came
// back to "Hi! What are we building today?" and thought the work was lost.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');

// ---------------------------------------------------------------- tiny DOM

function el(tag = 'div') {
  return {
    tagName: tag, className: '', textContent: '', style: {}, children: [], _html: '',
    set innerHTML(v) { this._html = v; this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    insertBefore(c, ref) { const i = this.children.indexOf(ref); this.children.splice(i < 0 ? 0 : i, 0, c); return c; },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); },
    querySelector(sel) {
      const cls = sel.replace(/^.*\./, '');
      return this.children.find((c) => c.className.split(' ').includes(cls)) || null;
    },
  };
}

let menuItems;
const events = [];
globalThis.window = { dispatchEvent(e) { events.push(e); } };
globalThis.CustomEvent = class { constructor(type, init) { this.type = type; this.detail = init?.detail; } };
globalThis.document = {
  createElement: el,
  querySelector: (sel) => (sel === '.menu-items' ? menuItems : null),
};

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

const { ThreadManager } = await import('../../frontend/chat/threads/ThreadManager.js');
const { MenuManager } = await import('../../frontend/chat/ui/MenuManager.js');
const { rememberThread, recallThread, startsFreshChat } = await import('../../frontend/chat/threads/lastThread.js');

function reset() {
  menuItems = el();
  events.length = 0;
  store.clear();
}

function jsonResponse(body, ok = true) {
  return { ok, status: ok ? 200 : 500, statusText: '', headers: { get: () => 'application/json' }, json: async () => body };
}

const thread = (id, title = id) => ({ thread_id: id, title, updated_at: new Date().toISOString() });
const titles = () => menuItems.children.filter((c) => c.className === 'menu-item').map((c) => c.children[0]?.textContent);

function manager() {
  const history = el();
  const renderer = { clearMessages() { history.children = []; }, getMessageHistory: () => history, addMessage(text) { history.appendChild(Object.assign(el(), { textContent: text })); } };
  return { tm: new ThreadManager(renderer, { closeMenu() {} }, { scrollToBottom() {} }), history };
}

// ---------------------------------------------------------------- menu refresh

test('opening the menu asks for the list again', () => {
  reset();
  const hamburger = Object.assign(el(), { classList: tokens() });
  const drawer = Object.assign(el(), { classList: tokens() });
  const mm = Object.create(MenuManager.prototype);
  Object.assign(mm, { hamburger, drawer });
  let opened = 0;
  mm.onOpen = () => { opened += 1; };

  mm.toggleMenu();          // open
  mm.toggleMenu();          // close: no fetch
  mm.openMenu();            // open
  assert.equal(opened, 2);
});

function tokens() {
  const s = new Set();
  return { toggle: (c) => (s.has(c) ? s.delete(c) : s.add(c)), add: (c) => s.add(c), remove: (c) => s.delete(c), contains: (c) => s.has(c) };
}

test('index.js wires the menu open and each finished turn to a list refresh', () => {
  assert.match(INDEX_JS, /menuManager\.onOpen\s*=\s*\(\)\s*=>\s*this\.threadManager\.fetchThreads\(\)/);
  assert.match(INDEX_JS, /type === 'end'\)\s*\{[^}]*threadManager\.fetchThreads\(\)/s);
});

test('a refresh shows a chat created after page load, without a loading flash', async () => {
  reset();
  const { tm } = manager();
  globalThis.fetch = async () => jsonResponse({ threads: [thread('old')], next_cursor: 'c1', has_more: false });
  await tm.fetchThreads();

  let sawLoading = false;
  globalThis.fetch = async () => {
    sawLoading = /Loading|\.\.\.<\/div>/.test(menuItems.innerHTML) || titles().length === 0;
    return jsonResponse({ threads: [thread('new', 'Drawing office plan'), thread('old')], next_cursor: 'c2', has_more: true });
  };
  await tm.fetchThreads();

  assert.equal(sawLoading, false, 'list was blanked while refreshing');
  assert.deepEqual(titles(), ['Drawing office plan', 'old']);
  assert.equal(tm.nextCursor, 'c2');
  assert.equal(tm.hasMore, true);
});

test('a failed refresh keeps the list on screen', async () => {
  reset();
  const { tm } = manager();
  globalThis.fetch = async () => jsonResponse({ threads: [thread('a'), thread('b')], next_cursor: null, has_more: false });
  await tm.fetchThreads();

  globalThis.fetch = async () => jsonResponse({}, false);
  await tm.fetchThreads();

  assert.deepEqual(titles(), ['a', 'b']);
  assert.ok(menuItems.querySelector('.threads-refresh-error'), 'no small error row');
});

test('a Load more page that lands after a refresh is dropped, so nothing shows twice', async () => {
  reset();
  const { tm } = manager();
  globalThis.fetch = async () => jsonResponse({ threads: [thread('t5')], next_cursor: 'c-old', has_more: true });
  await tm.fetchThreads();

  let releaseOldPage;
  globalThis.fetch = () => new Promise((r) => { releaseOldPage = () => r(jsonResponse({ threads: [thread('t4')], next_cursor: null, has_more: false })); });
  const loadMore = tm.loadMoreThreads();

  globalThis.fetch = async () => jsonResponse({ threads: [thread('t6'), thread('t5')], next_cursor: 'c-new', has_more: true });
  await tm.fetchThreads();
  releaseOldPage();
  await loadMore;

  assert.deepEqual(titles(), ['t6', 't5']);
  assert.equal(tm.nextCursor, 'c-new');
});

// ---------------------------------------------------------------- last chat

const changed = () => events.filter((e) => e.type === 'threadChanged').map((e) => e.detail.threadId);

test('a reload re-opens the remembered chat with its messages', async () => {
  reset();
  const { tm, history } = manager();
  rememberThread('t-last');
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    return jsonResponse([{ messages: [{ type: 'human', content: 'Drawing office plan' }] }]);
  };

  assert.equal(await tm.restoreLastThread(), true);
  assert.ok(urls[0].startsWith('/chat-history/t-last'));
  assert.deepEqual(changed(), ['t-last']);
  assert.equal(history.children[0].textContent, 'Drawing office plan');
});

test('a remembered chat that is gone falls back to a blank chat, quietly', async () => {
  reset();
  const { tm } = manager();
  rememberThread('t-gone');
  globalThis.fetch = async () => jsonResponse([{ messages: [] }]);

  assert.equal(await tm.restoreLastThread(), false);
  assert.deepEqual(changed(), []);

  globalThis.fetch = async () => { throw new Error('offline'); };
  assert.equal(await tm.restoreLastThread(), false);
});

test('nothing remembered means no request at all', async () => {
  reset();
  const { tm } = manager();
  let called = false;
  globalThis.fetch = async () => { called = true; return jsonResponse([]); };
  assert.equal(await tm.restoreLastThread(), false);
  assert.equal(called, false);
});

test('a chat the user started meanwhile is never swapped out', async () => {
  reset();
  const { tm } = manager();
  rememberThread('t-last');
  globalThis.fetch = async () => jsonResponse([{ messages: [{ type: 'human', content: 'x' }] }]);

  assert.equal(await tm.restoreLastThread({ shouldApply: () => false }), false);
  assert.deepEqual(changed(), []);
});

test('the remembered id follows threadChanged and the first send', () => {
  assert.match(INDEX_JS, /'threadChanged'[\s\S]{0,200}rememberThread\(e\.detail\.threadId\)/);
  assert.match(INDEX_JS, /ensureThreadId\(\);\s*\n\s*rememberThread\(threadId\)/);
});

test('URL params that start a chat beat the remembered one', () => {
  for (const p of ['prompt=hi', 'welcome_prompt=hi', 'conversation=abc']) {
    assert.equal(startsFreshChat(new URLSearchParams(p)), true, p);
  }
  assert.equal(startsFreshChat(new URLSearchParams('llm_model=x&agent_mode=engineer')), false);
  assert.match(INDEX_JS, /startsFreshChat\(/);
});

test('blocked storage never breaks the chat', () => {
  const saved = globalThis.localStorage;
  globalThis.localStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  try {
    rememberThread('x');
    assert.equal(recallThread(), null);
  } finally {
    globalThis.localStorage = saved;
  }
});
