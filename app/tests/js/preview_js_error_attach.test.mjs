// Tests for the preview JavaScript-error tray (ui/ErrorAttach.js).
//
// The chat page cannot read the Rails preview's errors directly — different
// origin — so the app PUSHES them over postMessage and this class receives them.
// The properties that matter: only the configured origin can put entries in a
// tray the user will paste into a prompt, repeats collapse instead of flooding,
// the tray is capped, the default-checked "Show Leo" box sends the errors with
// the next message (and clears them once sent), unchecking it holds them back,
// and the rendered HTML escapes app-controlled error text.

import assert from 'node:assert/strict';
import test from 'node:test';

import { ErrorAttach, friendlySummary } from '../../frontend/chat/ui/ErrorAttach.js';

const RAILS_ORIGIN = 'https://rails-box.example.com';

// ---------------------------------------------------------------------------
// Minimal DOM stubs (no jsdom dependency, matching the other js tests)
// ---------------------------------------------------------------------------

class FakeClassList {
  constructor() { this._set = new Set(); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
}

class FakeElement {
  constructor(className = '') {
    this.className = className;
    this.classList = new FakeClassList();
    this.innerHTML = '';
    this.children = [];
    this.isConnected = true;
    this.handlers = {};
    this.title = '';
  }
  addEventListener(t, fn) { (this.handlers[t] ||= []).push(fn); }
  querySelectorAll() { return []; }   // handler wiring is not under test here
  querySelector() { return new FakeElement(); }
  appendChild(el) { this.children.push(el); el.parent = this; return el; }
  insertBefore(el) { this.children.push(el); el.parent = this; return el; }
  remove() {
    this.isConnected = false;
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
  }
  focus() {}
}

function harness() {
  const listeners = [];
  globalThis.window = { addEventListener: (t, fn) => { if (t === 'message') listeners.push(fn); } };
  const body = new FakeElement('body');
  globalThis.document = {
    createElement: () => new FakeElement(),
    body,
    addEventListener() {},
    removeEventListener() {},
  };

  const banner = new FakeElement('js-error-banner');
  const input = new FakeElement('message-input');
  input.parentElement = new FakeElement('composer');

  const attach = new ErrorAttach({ getAllowedOrigin: () => RAILS_ORIGIN });
  attach.init(banner, input);

  // Deliver a postMessage exactly as the browser would.
  const send = (origin, data) => listeners.forEach((fn) => fn({ origin, data }));
  const sendError = (error, origin = RAILS_ORIGIN) =>
    send(origin, { source: 'llamapress', type: 'js-error', error });

  return { attach, banner, input, body, send, sendError };
}

function anError(overrides = {}) {
  return {
    id: 'e1',
    kind: 'uncaught',
    message: 'TypeError: x is not a function',
    stack: 'at foo (app.js:1:1)',
    path: '/posts/3',
    timestamp: 1,
    ...overrides
  };
}

// ---------------------------------------------------------------------------
// Receiving + the origin guard
// ---------------------------------------------------------------------------

test('records an error pushed from the configured Rails origin', () => {
  const { attach, sendError } = harness();
  sendError(anError());
  assert.equal(attach.errors.length, 1);
  assert.equal(attach.errors[0].message, 'TypeError: x is not a function');
  assert.equal(attach.showLeo, true);
});

test('rejects an error pushed from any other origin', () => {
  const { attach, sendError } = harness();
  sendError(anError(), 'https://evil.example.com');
  assert.equal(attach.errors.length, 0);
});

test('ignores unrelated postMessages', () => {
  const { attach, send } = harness();
  send(RAILS_ORIGIN, { source: 'llamapress', type: 'console-logs', logs: [] });
  send(RAILS_ORIGIN, { source: 'leonardo', type: 'js-error', error: anError() });
  send(RAILS_ORIGIN, null);
  send(RAILS_ORIGIN, 'a string');
  assert.equal(attach.errors.length, 0);
});

test('a getAllowedOrigin that throws drops the message instead of cascading', () => {
  const listeners = [];
  globalThis.window = { addEventListener: (t, fn) => { if (t === 'message') listeners.push(fn); } };
  globalThis.document = { createElement: () => new FakeElement() };
  const banner = new FakeElement();
  const input = new FakeElement();
  input.parentElement = new FakeElement();
  const attach = new ErrorAttach({ getAllowedOrigin: () => { throw new Error('no window'); } });
  attach.init(banner, input);
  listeners.forEach((fn) => fn({ origin: RAILS_ORIGIN, data: { source: 'llamapress', type: 'js-error', error: anError() } }));
  assert.equal(attach.errors.length, 0);
});

// ---------------------------------------------------------------------------
// Dedupe + cap
// ---------------------------------------------------------------------------

test('repeats of the same error on the same page collapse into a count', () => {
  const { attach, sendError } = harness();
  for (let i = 0; i < 50; i++) sendError(anError({ id: `e${i}`, timestamp: i }));
  assert.equal(attach.errors.length, 1);
  assert.equal(attach.errors[0].count, 50);
});

test('the same message on a different page is a separate row', () => {
  const { attach, sendError } = harness();
  sendError(anError({ id: 'a', path: '/posts/3' }));
  sendError(anError({ id: 'b', path: '/posts/4' }));
  assert.equal(attach.errors.length, 2);
});

test('the tray is capped and keeps the newest errors', () => {
  const { attach, sendError } = harness();
  for (let i = 0; i < 40; i++) sendError(anError({ id: `e${i}`, message: `boom ${i}` }));
  assert.equal(attach.errors.length, 25);
  assert.equal(attach.errors.at(-1).message, 'boom 39');
});

test('an error with no message is ignored', () => {
  const { attach, sendError } = harness();
  sendError({ id: 'x', kind: 'uncaught' });
  assert.equal(attach.errors.length, 0);
});

// ---------------------------------------------------------------------------
// Banner visibility
// ---------------------------------------------------------------------------

test('the banner stays hidden until an error arrives, and dismiss re-hides it', () => {
  const { attach, banner, sendError } = harness();
  assert.equal(banner.classList.contains('hidden'), true);

  sendError(anError());
  assert.equal(banner.classList.contains('hidden'), false);

  attach.dismiss();
  assert.equal(banner.classList.contains('hidden'), true);
});

test('a NEW error reopens a dismissed banner, a repeat does not', () => {
  const { attach, banner, sendError } = harness();
  sendError(anError());
  attach.dismiss();

  sendError(anError({ id: 'dupe' }));            // same message+path -> still dismissed
  assert.equal(banner.classList.contains('hidden'), true);

  sendError(anError({ id: 'e2', message: 'ReferenceError: y' }));
  assert.equal(banner.classList.contains('hidden'), false);
});

// ---------------------------------------------------------------------------
// "Show Leo" — the whole attach interaction
// ---------------------------------------------------------------------------

test('Show Leo is checked by default, so errors ride along with no extra click', () => {
  const { attach, sendError } = harness();
  sendError(anError());
  assert.equal(attach.showLeo, true);
  assert.match(attach.buildMessageBlock(), /TypeError: x is not a function/);
});

test('unchecking Show Leo holds the errors back', () => {
  const { attach, sendError } = harness();
  sendError(anError());
  attach.toggleShowLeo();
  assert.equal(attach.showLeo, false);
  assert.equal(attach.buildMessageBlock(), '');
});

test('re-checking Show Leo puts them back in play', () => {
  const { attach, sendError } = harness();
  sendError(anError());
  attach.toggleShowLeo();
  attach.toggleShowLeo();
  assert.match(attach.buildMessageBlock(), /TypeError/);
});

test('the Show Leo choice survives new errors arriving', () => {
  // Unchecking is a deliberate "not this time" — a later error must not silently
  // re-arm sending without the user noticing.
  const { attach, sendError } = harness();
  sendError(anError({ id: 'a', message: 'boom a' }));
  attach.toggleShowLeo();
  sendError(anError({ id: 'b', message: 'boom b' }));
  assert.equal(attach.showLeo, false);
  assert.equal(attach.buildMessageBlock(), '');
});

test('every error in the tray goes, not just the newest', () => {
  const { attach, sendError } = harness();
  sendError(anError({ id: 'a', message: 'boom a' }));
  sendError(anError({ id: 'b', message: 'boom b' }));
  const block = attach.buildMessageBlock();
  assert.match(block, /boom a/);
  assert.match(block, /boom b/);
});

test('dismissing the banner also stops the errors being sent', () => {
  // The × is the user saying "not interested" — it must not quietly keep
  // shipping the errors on the next message.
  const { attach, sendError } = harness();
  sendError(anError());
  attach.dismiss();
  assert.equal(attach.buildMessageBlock(), '');
});

// ---------------------------------------------------------------------------
// The wire format
// ---------------------------------------------------------------------------

test('no errors produces no message block', () => {
  const { attach } = harness();
  assert.equal(attach.buildMessageBlock(), '');
});

test('the message block carries message, stack, path and repeat count', () => {
  const { attach, sendError } = harness();
  sendError(anError());
  sendError(anError({ id: 'again' }));   // -> count 2

  const block = attach.buildMessageBlock();
  assert.match(block, /^<PAGE_JS_ERRORS>/);
  assert.match(block, /<\/PAGE_JS_ERRORS>$/);
  assert.match(block, /TypeError: x is not a function/);
  assert.match(block, /at foo \(app\.js:1:1\)/);
  assert.match(block, /on \/posts\/3/);
  assert.match(block, /\(x2\)/);
  assert.match(block, /error from their app preview/);   // singular
});

test('the message block pluralises for multiple errors', () => {
  const { attach, sendError } = harness();
  sendError(anError({ id: 'a', message: 'boom a' }));
  sendError(anError({ id: 'b', message: 'boom b' }));
  assert.match(attach.buildMessageBlock(), /errors from their app preview/);
});

test('sending with Show Leo checked clears the tray and hides the banner', () => {
  const { attach, banner, sendError } = harness();
  sendError(anError());
  attach.clear();                       // index.js calls this after a send
  assert.equal(attach.errors.length, 0);
  assert.equal(attach.buildMessageBlock(), '');
  assert.equal(banner.classList.contains('hidden'), true);
});

test('sending with Show Leo unchecked leaves the errors alone', () => {
  // Nothing was sent, so nothing should be consumed — the user can change their
  // mind on the next message.
  const { attach, sendError } = harness();
  sendError(anError());
  attach.toggleShowLeo();
  attach.clear();
  assert.equal(attach.errors.length, 1);
});

// ---------------------------------------------------------------------------
// Rendering safety
// ---------------------------------------------------------------------------

test('app-controlled error text is escaped before it reaches innerHTML', () => {
  const { attach, body, sendError } = harness();
  sendError(anError({ message: '<img src=x onerror="alert(1)">' }));
  attach.openDetails();

  const modal = body.children.at(-1);
  assert.ok(!modal.innerHTML.includes('<img src=x'), 'raw tag must not survive');
  assert.ok(modal.innerHTML.includes('&lt;img'), 'tag must be escaped');
});

test('the banner is one compact line: count, Read more, and a Show Leo box', () => {
  const { banner, sendError } = harness();
  sendError(anError());

  assert.match(banner.innerHTML, /1 error detected/);
  assert.match(banner.innerHTML, /Read more/);
  assert.match(banner.innerHTML, /Show Leo/);
  assert.match(banner.innerHTML, /type="checkbox"[^>]*checked/);
  // No expand/attach affordances any more — that was the bulky version.
  assert.ok(!/data-act="toggle"/.test(banner.innerHTML));
  assert.ok(!/data-act="attach"/.test(banner.innerHTML));
});

// ---------------------------------------------------------------------------
// Plain-language summaries
// ---------------------------------------------------------------------------

test('common failures get a plain-language summary, not developer jargon', () => {
  const cases = [
    ['uncaught', 'Uncaught ReferenceError: doThing is not defined', /doesn't exist/],
    ['uncaught', 'TypeError: cart.items is not iterable', /can't be used/],
    ['uncaught', 'SyntaxError: Unexpected token', /mistake in/],
    ['unhandled-rejection', 'Failed to fetch', /server/],
    ['unhandled-rejection', 'something odd', /background task/],
  ];
  for (const [kind, message, expected] of cases) {
    assert.match(friendlySummary({ kind, message }), expected, `${kind}: ${message}`);
  }
});

test('an unrecognised error still gets a sentence, never an empty string', () => {
  const s = friendlySummary({ kind: 'uncaught', message: 'ლ(ಠ益ಠ)ლ' });
  assert.ok(s.length > 0);
  assert.doesNotMatch(s, /undefined/);
});

test('friendlySummary survives junk', () => {
  assert.ok(friendlySummary({}).length > 0);
  assert.ok(friendlySummary(null).length > 0);
});

// ---------------------------------------------------------------------------
// The "Read more" popup
// ---------------------------------------------------------------------------

test('Read more opens a popup listing every error', () => {
  const { attach, body, sendError } = harness();
  sendError(anError({ id: 'a', message: 'TypeError: cart.items is not iterable' }));
  sendError(anError({ id: 'b', message: 'ReferenceError: doThing is not defined' }));

  attach.openDetails();
  const modal = body.children.at(-1);
  assert.match(modal.innerHTML, /cart\.items is not iterable/);
  assert.match(modal.innerHTML, /doThing is not defined/);
  // Both the plain-language line AND the technical detail are present.
  // (Apostrophes are HTML-escaped in the markup, so match around them.)
  assert.match(modal.innerHTML, /in a way it can.{0,6}t be used/);
  assert.match(modal.innerHTML, /something that does.{0,6}t exist/);
});

test('the popup shows the stack and where it happened', () => {
  const { attach, body, sendError } = harness();
  sendError(anError());
  attach.openDetails();

  const modal = body.children.at(-1);
  assert.match(modal.innerHTML, /at foo \(app\.js:1:1\)/);
  assert.match(modal.innerHTML, /\/posts\/3/);
});

test('the popup tells you what happens next, based on Show Leo', () => {
  const { attach, body, sendError } = harness();
  sendError(anError());

  attach.openDetails();
  assert.match(body.children.at(-1).innerHTML, /next message/);
  attach.closeDetails();

  attach.toggleShowLeo();
  attach.openDetails();
  assert.match(body.children.at(-1).innerHTML, /Show Leo/);
});

test('closing the popup removes it from the page', () => {
  const { attach, body, sendError } = harness();
  sendError(anError());

  attach.openDetails();
  const before = body.children.length;
  attach.closeDetails();
  assert.equal(body.children.length, before - 1);
});

test('opening twice does not stack two popups', () => {
  const { attach, body, sendError } = harness();
  sendError(anError());

  attach.openDetails();
  const after1 = body.children.length;
  attach.openDetails();
  assert.equal(body.children.length, after1);
});

test('the popup closes itself when the errors are sent', () => {
  const { attach, body, sendError } = harness();
  sendError(anError());
  attach.openDetails();
  const before = body.children.length;

  attach.clear();                       // index.js calls this after a send
  assert.equal(body.children.length, before - 1);
});

test('the count pluralises', () => {
  const { banner, sendError } = harness();
  sendError(anError({ id: 'a', message: 'boom a' }));
  sendError(anError({ id: 'b', message: 'boom b' }));
  assert.match(banner.innerHTML, /2 errors detected/);
});

test('repeats count toward the total', () => {
  const { banner, sendError } = harness();
  sendError(anError());
  sendError(anError({ id: 'again' }));
  assert.match(banner.innerHTML, /2 errors detected/);
});
