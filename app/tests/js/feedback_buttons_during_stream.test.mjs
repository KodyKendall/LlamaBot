// 👍/👎 must be clickable on an assistant bubble WHILE the run is still
// streaming (0.7.9).
//
// Asked for by Darren: "the thumbs down/thumbs up only works AFTER the
// messages/stream has stopped, but I want them to show up as available ... even
// when it's still running." The single best moment to rate a reply is the moment
// the user sees it go wrong, which is mid-run; by the time the stream ends they
// have moved on, and every rating lost there is an eval signal we never get.
//
// Two things made it end-of-stream only:
//   1. addCopyButton() was called only on a bubble that already HAS text —
//      renderAiMessage's `if (safeContent)`, and finalizeAiMessages() which
//      skips empty bubbles and runs from handleEndMessage().
//   2. handleTextContent() re-renders with `innerHTML =` on EVERY chunk, which
//      destroys any button appended to the bubble. So calling addCopyButton
//      earlier is not enough on its own — the buttons must live in a sibling
//      node the stream never rewrites.
//
// The mini-DOM below models `innerHTML =` destroying children on purpose. That
// is the actual regression; a stub that kept them would pass while the browser
// broke.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

// ---------------------------------------------------------------------------
// Mini-DOM
// ---------------------------------------------------------------------------

class El {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this._attrs = {};
    this._html = '';
    this._listeners = {};
    this.className = '';
    this.textContent = '';
    this.style = {};
    this.id = '';
    this.title = '';
    this.onclick = null;
    this.classList = {
      _s: new Set(),
      add: (...n) => n.forEach((x) => this.classList._s.add(x)),
      remove: (...n) => n.forEach((x) => this.classList._s.delete(x)),
      contains: (n) => this.classList._s.has(n),
    };
  }

  // The load-bearing part: assigning innerHTML blows away child nodes, exactly
  // like the browser. Buttons appended to a streamed bubble do not survive it.
  get innerHTML() { return this._html; }
  set innerHTML(v) {
    this._html = String(v);
    this.children.forEach((c) => { c.parentElement = null; });
    this.children = [];
  }

  setAttribute(k, v) { this._attrs[k] = String(v); }
  getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; }
  hasAttribute(k) { return k in this._attrs; }
  removeAttribute(k) { delete this._attrs[k]; }

  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  insertBefore(c) { c.parentElement = this; this.children.unshift(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentElement = null; }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }

  get firstChild() { return this.children[0] || null; }

  _matches(sel) {
    const s = sel.trim();
    const attr = /^\[([a-zA-Z-]+)(?:="([^"]*)")?\]$/.exec(s);
    if (attr) {
      const [, name, val] = attr;
      if (val === undefined) return this.hasAttribute(name);
      return this.getAttribute(name) === val;
    }
    if (s.startsWith('.')) return this.classList.contains(s.slice(1));
    return this.tagName === s.toUpperCase();
  }

  matches(sel) { return sel.split(',').some((p) => this._matches(p)); }

  _descendants(out = []) {
    for (const c of this.children) { out.push(c); c._descendants(out); }
    return out;
  }

  querySelectorAll(sel) {
    // Only the leaf of a descendant selector is needed by the code under test.
    const leaf = sel.split(',').map((p) => p.trim().split(/\s+/).pop()).join(',');
    return this._descendants().filter((e) => e.matches(leaf));
  }

  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }

  closest(sel) {
    let n = this;
    while (n) { if (n.matches(sel)) return n; n = n.parentElement; }
    return null;
  }

  addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); }

  /** Bubbling dispatch — the feedback/reply handlers use delegation. */
  dispatch(type, target = this) {
    const evt = { type, target, stopPropagation() {}, preventDefault() {} };
    let n = this;
    while (n) {
      (n._listeners[type] || []).forEach((fn) => fn(evt));
      n = n.parentElement;
    }
  }

  click() {
    if (this.onclick) this.onclick({ stopPropagation() {}, target: this });
    this.dispatch('click', this);
  }
}

function installDom() {
  globalThis.document = {
    createElement: (t) => new El(t),
    querySelector: () => null,
    addEventListener: () => {},
    getElementById: () => null,
  };
  globalThis.window = globalThis;
  globalThis.window.prompt = () => null;
  globalThis.navigator = { clipboard: { writeText: () => Promise.resolve() } };
  globalThis.CustomEvent = class { constructor(t, o = {}) { this.type = t; Object.assign(this, o); } };
  globalThis.window.dispatchEvent = () => {};
}

installDom();

const { MessageRenderer } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'messages', 'MessageRenderer.js')
);

// ---------------------------------------------------------------------------
// Harness: a renderer plus the two chunk paths the socket drives
// ---------------------------------------------------------------------------

function makeRenderer() {
  const history = new El();
  const posts = [];
  globalThis.fetch = (url, opts) => {
    posts.push({ url, body: JSON.parse(opts.body) });
    return Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
  };
  const appState = { getThreadId: () => 'thread-1' };
  const r = new MessageRenderer(history, null, null, null, null, {}, null, {}, null, appState);
  return { r, history, posts };
}

const CHUNK = (id) => ({ id, type: 'AIMessageChunk' });

/** What MessageHandler.handleTextContent does to the bubble on every chunk. */
function streamChunk(renderer, bubble, fullText) {
  const body = bubble.querySelector('[data-llamabot="message-body"]') || bubble;
  body.innerHTML = renderer.markdownParser.parse(fullText);
  bubble.setAttribute('data-raw-content', fullText);
}

const actions = (b) => b.querySelectorAll('[data-llamabot="thumb-up-btn"]');
const downs = (b) => b.querySelectorAll('[data-llamabot="thumb-down-btn"]');

// ---------------------------------------------------------------------------

test('an empty streaming bubble already has 👍/👎', () => {
  const { r } = makeRenderer();
  // This is exactly how the socket opens a bubble: empty content, chunk data.
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));

  assert.equal(actions(bubble).length, 1, 'thumb-up should exist before any text arrives');
  assert.equal(downs(bubble).length, 1);
});

test('the buttons survive every chunk, not just the first', () => {
  const { r } = makeRenderer();
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));

  streamChunk(r, bubble, 'Hel');
  assert.equal(actions(bubble).length, 1, 'gone after chunk 1');
  streamChunk(r, bubble, 'Hello ');
  assert.equal(actions(bubble).length, 1, 'gone after chunk 2');
  streamChunk(r, bubble, 'Hello world');
  assert.equal(actions(bubble).length, 1, 'gone after chunk 3 — innerHTML= ate them');
});

test('the streamed text still lands in the bubble', () => {
  const { r } = makeRenderer();
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));
  streamChunk(r, bubble, 'Hello world');

  const body = bubble.querySelector('[data-llamabot="message-body"]');
  assert.ok(body, 'a message-body node should exist for the stream to write into');
  assert.match(body.innerHTML, /Hello world/);
  assert.equal(bubble.getAttribute('data-raw-content'), 'Hello world');
});

test('clicking 👍 mid-stream posts message-scoped feedback with a key', () => {
  const { r, posts } = makeRenderer();
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));
  streamChunk(r, bubble, 'Half a sen');

  bubble.querySelector('[data-llamabot="thumb-up-btn"]').click();

  assert.equal(posts.length, 1, 'the rating should be sent while the run is still going');
  assert.equal(posts[0].url, '/api/feedback');
  assert.equal(posts[0].body.scope, 'message');
  assert.equal(posts[0].body.rating, 'good');
  assert.ok(posts[0].body.message_key, 'message_key must be non-empty — mid-stream, content cannot identify the row');
});

test('a chunk with no provider id still yields a usable key', () => {
  // Verified against langchain: AIMessageChunk.id is stable across chunks AND
  // survives merging when the provider sends one — but it is None on every
  // chunk when the provider omits it. A rating must still be attributable.
  const { r, posts } = makeRenderer();
  const bubble = r.addMessage('', 'ai', { type: 'AIMessageChunk' });
  streamChunk(r, bubble, 'text');

  bubble.querySelector('[data-llamabot="thumb-down-btn"]').click();

  assert.equal(posts.length, 1);
  assert.ok(posts[0].body.message_key, 'a minted key beats no key at all');
});

test('the key does not change as the stream advances', () => {
  const { r, posts } = makeRenderer();
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));
  const atCreation = bubble.getAttribute('data-message-key');

  streamChunk(r, bubble, 'one');
  streamChunk(r, bubble, 'one two');
  streamChunk(r, bubble, 'one two three');

  assert.equal(bubble.getAttribute('data-message-key'), atCreation);
  bubble.querySelector('[data-llamabot="thumb-up-btn"]').click();
  assert.equal(posts[0].body.message_key, atCreation);
});

test('end of stream does not add a second control row', () => {
  const { r } = makeRenderer();
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));
  streamChunk(r, bubble, 'Hello world');

  r.finalizeAiMessages();

  assert.equal(actions(bubble).length, 1, 'finalizeAiMessages duplicated the row');
  assert.equal(bubble.querySelectorAll('[data-llamabot="copy-btn"]').length, 1);
});

test('a stream resume does not add a second control row either', () => {
  const { r } = makeRenderer();
  const bubble = r.addMessage('', 'ai', CHUNK('chatcmpl-abc'));
  streamChunk(r, bubble, 'the last 435 chars');

  // Reconnect hands us the authoritative full text.
  r.replaceLastAiMessage('the whole 906-char answer');

  assert.equal(actions(bubble).length, 1, 'replaceLastAiMessage duplicated the row');
  const body = bubble.querySelector('[data-llamabot="message-body"]');
  assert.match(body.innerHTML, /whole 906-char answer/);
  assert.equal(bubble.getAttribute('data-raw-content'), 'the whole 906-char answer');
});

test('a normal non-streamed assistant message is unaffected', () => {
  const { r } = makeRenderer();
  const bubble = r.addMessage('Here is the answer.', 'ai', { id: 'm1' });

  assert.equal(actions(bubble).length, 1);
  const body = bubble.querySelector('[data-llamabot="message-body"]');
  assert.match(body.innerHTML, /Here is the answer/);
  assert.equal(bubble.getAttribute('data-raw-content'), 'Here is the answer.');
});

test('copy still reads the raw markdown off the outer bubble', () => {
  // setupFeedbackHandler, setupReplyHandler and ClipboardFormatter all reach it
  // with closest('[data-raw-content]'). It must stay on the bubble, not move
  // into the body node.
  const { r } = makeRenderer();
  const bubble = r.addMessage('# Title', 'ai', { id: 'm1' });

  assert.equal(bubble.getAttribute('data-raw-content'), '# Title');
  const btn = bubble.querySelector('[data-llamabot="copy-btn"]');
  assert.equal(btn.closest('[data-raw-content]'), bubble);
});
