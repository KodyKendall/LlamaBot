// A finished turn keeps ONE "thinking..." row, not one per step (0.7.14).
//
// Each reasoning step between tool calls opens its own thinking row. Stacking
// them while Leo works is fine, but they used to stay after the answer landed —
// 9 rows of dead space between the question and the reply. On the end frame
// they now fold into the last row, which keeps every step's text.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

// Mini-DOM: just enough for renderThinkingMessage. Setting innerHTML pulls out
// the .thinking-message-text body so the merged text can be asserted.
class El {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentElement = null;
    this._attrs = {};
    this.className = '';
    this.textContent = '';
    this.id = '';
    this.classList = { add() {}, remove() {}, contains: () => false };
  }

  get innerHTML() { return this.textContent; }
  set innerHTML(html) {
    this.children = [];
    const m = /<div class="thinking-message-text">([\s\S]*?)<\/div>/.exec(html);
    if (m) {
      const t = new El();
      t.className = 'thinking-message-text';
      t.textContent = m[1];
      this.appendChild(t);
    }
  }

  setAttribute(k, v) { this._attrs[k] = String(v); }
  getAttribute(k) { return this._attrs[k] ?? null; }
  addEventListener() {}
  appendChild(c) { c.parentElement = this; this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter((x) => x !== c); c.parentElement = null; }
  remove() { if (this.parentElement) this.parentElement.removeChild(this); }

  _all(out = []) { for (const c of this.children) { out.push(c); c._all(out); } return out; }
  querySelector(sel) {
    return this._all().find((e) => sel === `.${e.className}`) || null;
  }
}

const history = new El();
globalThis.document = {
  createElement: (t) => new El(t),
  querySelector: () => null,
  addEventListener: () => {},
  getElementById: (id) => history._all().find((e) => e.id === id) || null,
};
globalThis.window = globalThis;
globalThis.CustomEvent = class { constructor(t, o = {}) { this.type = t; Object.assign(this, o); } };
globalThis.window.dispatchEvent = () => {};

const { MessageRenderer } = await import(resolve(APP_ROOT, 'frontend/chat/messages/MessageRenderer.js'));
const { MessageHandler } = await import(resolve(APP_ROOT, 'frontend/chat/websocket/MessageHandler.js'));

const thinkingRows = () => history.children.filter((c) => c.getAttribute('data-llamabot') === 'thinking-message');

function makeHandler() {
  const appState = {
    getThreadId: () => 'thread-1',
    setCurrentAiMessage() {},
    getFormattedElapsedTime: () => '0s',
  };
  const renderer = new MessageRenderer(history, null, null, null, null, {}, null, {}, null, appState);
  renderer.handleEndMessage = () => {};
  const handler = new MessageHandler(appState, {}, renderer, { removeStreamingOverlay() {} }, null, null, {});
  return handler;
}

// Distinct row ids: the handler keys them off Date.now(), which a test runs inside of.
let now = 1_000;
Date.now = () => (now += 1);

test('three thinking steps stack mid-run, then fold into one row on end', () => {
  history.children = [];
  const handler = makeHandler();

  for (const step of ['plan the query', 'read the spend table', 'sum it up']) {
    handler.handleThinkingContent(step);
    handler.finalizeCurrentThinking(); // a tool call / text arrives between steps
  }
  assert.equal(thinkingRows().length, 3, 'stacking while Leo works is allowed');

  handler.handleMessage({ type: 'end' });

  const rows = thinkingRows();
  assert.equal(rows.length, 1, 'only one thinking row survives the finished turn');
  const text = rows[0].querySelector('.thinking-message-text').textContent;
  for (const step of ['plan the query', 'read the spend table', 'sum it up']) {
    assert.ok(text.includes(step), `merged row keeps "${step}"`);
  }
});

test('the next turn starts fresh and does not touch the previous turn\'s row', () => {
  history.children = [];
  const handler = makeHandler();

  handler.handleThinkingContent('turn one');
  handler.handleMessage({ type: 'end' });
  handler.handleThinkingContent('turn two a');
  handler.finalizeCurrentThinking();
  handler.handleThinkingContent('turn two b');
  handler.handleMessage({ type: 'end' });

  const texts = thinkingRows().map((r) => r.querySelector('.thinking-message-text').textContent);
  assert.equal(texts.length, 2, 'one row per finished turn');
  assert.equal(texts[0], 'turn one');
});
