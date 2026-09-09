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

// A browser extension (MetaMask, a password manager, a request interceptor)
// injects its scripts into the main world of the preview iframe, so its throws
// fire the app's own listeners and arrive here from the correct Rails origin.
// The Rails overlay drops these at the source, but boxes whose overlay lags the
// image need the same guard on this side. The origin is right; only the URL
// scheme in the frames says whose code it was.
test('an error whose every frame is a browser extension is not recorded', () => {
  const { attach, sendError } = harness();
  sendError(anError({
    kind: 'unhandled-rejection',
    message: 'Failed to connect to MetaMask',
    stack: 'i: Failed to connect to MetaMask\n'
         + '    at Object.connect (chrome-extension://nkbihfbeogaeaoehlefnkodbefgpgknn/scripts/inpage.js:7:84292)'
  }));
  assert.equal(attach.errors.length, 0);
});

test('an extension frame on top of app frames is still recorded', () => {
  // The extension monkey-patched window.fetch, so it is the top frame — but
  // "Failed to fetch" was a real problem in the app's own Turbo underneath.
  const { attach, sendError } = harness();
  sendError(anError({
    kind: 'console.error',
    message: 'TypeError: Failed to fetch',
    stack: 'TypeError: Failed to fetch\n'
         + '    at s.fetch (chrome-extension://eppiocemhmnlbhjplcgkofciiegomcon/libs/requests.js:1:3633)\n'
         + '    at $ (https://rails-leo-mevve.leo.llamapress.ai/assets/turbo.min-38d0308.js:5:8309)\n'
         + '    at X.perform (https://rails-leo-mevve.leo.llamapress.ai/assets/turbo.min-38d0308.js:5:10084)'
  }));
  assert.equal(attach.errors.length, 1);
});

test('a bare "Script error." is kept — that is a CORS rule, not an extension', () => {
  const { attach, sendError } = harness();
  sendError(anError({ message: 'Script error. at :0', stack: null }));
  assert.equal(attach.errors.length, 1);
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

  assert.match(banner.innerHTML, /1 JavaScript error detected/);
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
  assert.match(banner.innerHTML, /2 JavaScript errors detected/);
});

test('a repeat of the same error stays one problem in the notice', () => {
  // The count is how many times it fired, not how many things are broken. The
  // popup carries "happened N times" on the entry itself; the notice would only
  // be alarming if it turned one broken page into "50 errors detected".
  const { attach, banner, sendError } = harness();
  sendError(anError());
  sendError(anError({ id: 'again' }));

  assert.match(banner.innerHTML, /1 JavaScript error detected/);
  assert.equal(attach.errors[0].count, 2);
});

test('two different errors are two problems', () => {
  const { banner, sendError } = harness();
  sendError(anError({ id: 'a', message: 'boom a' }));
  sendError(anError({ id: 'b', message: 'boom b' }));
  assert.match(banner.innerHTML, /2 JavaScript errors detected/);
});

// ---------------------------------------------------------------------------
// Rails server errors, in the same tray
// ---------------------------------------------------------------------------
//
// Same notice, same "Show Leo" box, second source. These do not arrive over
// postMessage — the Rails app cannot reach this page — they come from
// ui/RailsErrorPoll.js, which reads them through LlamaBot. From here on they are
// ordinary tray entries, distinguished only by `kind`.

function aServerError(overrides = {}) {
  return {
    id: 'rails-5',
    kind: 'rails',
    message: "NoMethodError: undefined method `title' for nil",
    path: 'GET /posts/1',
    count: 1,
    stack: 'app/views/posts/show.html.erb:3',
    ...overrides
  };
}

test('a server error lands in the same tray as a JS error', () => {
  const { attach, banner, sendError } = harness();
  sendError(anError());
  attach.record(aServerError());

  assert.equal(attach.errors.length, 2);
  assert.match(banner.innerHTML, /1 Rails error, 1 JavaScript error detected/);
});

test('a server error carries the times Rails raised it', () => {
  // The gem collapses a render loop into a count rather than 200 rows, so the
  // count is the only place that information exists.
  const { attach } = harness();
  attach.record(aServerError({ count: 12 }));
  assert.equal(attach.errors[0].count, 12);
});

test('re-reporting the same server error takes the higher count, not one more', () => {
  // The feed re-stamps an entry when it repeats, so the same crash can arrive
  // twice carrying its running total. Incrementing would double-count it.
  const { attach } = harness();
  attach.record(aServerError({ count: 3 }));
  attach.record(aServerError({ count: 5 }));

  assert.equal(attach.errors.length, 1);
  assert.equal(attach.errors[0].count, 5);
});

test('a JS error with no count still increments on a repeat', () => {
  const { attach, sendError } = harness();
  sendError(anError());
  sendError(anError({ id: 'again' }));
  assert.equal(attach.errors[0].count, 2);
});

test('server errors go to Leo under their own tag', () => {
  // The JS block says "JavaScript error ... from their app preview", which would
  // be a lie about a Ruby backtrace — and the two want different fixes.
  const { attach, sendError } = harness();
  sendError(anError());
  attach.record(aServerError());

  const block = attach.buildMessageBlock();
  assert.match(block, /<PAGE_JS_ERRORS>[\s\S]*TypeError[\s\S]*<\/PAGE_JS_ERRORS>/);
  assert.match(block, /<RAILS_SERVER_ERRORS>[\s\S]*NoMethodError[\s\S]*<\/RAILS_SERVER_ERRORS>/);
  const jsBlock = block.match(/<PAGE_JS_ERRORS>([\s\S]*?)<\/PAGE_JS_ERRORS>/)[1];
  assert.ok(!jsBlock.includes('NoMethodError'), 'no Ruby in the JS block');
});

test('a tray with only server errors sends no JavaScript block', () => {
  const { attach } = harness();
  attach.record(aServerError());

  const block = attach.buildMessageBlock();
  assert.ok(!block.includes('<PAGE_JS_ERRORS>'));
  assert.match(block, /<RAILS_SERVER_ERRORS>/);
});

test('a tray with only JS errors is unchanged', () => {
  const { attach, sendError } = harness();
  sendError(anError());

  const block = attach.buildMessageBlock();
  assert.match(block, /<PAGE_JS_ERRORS>/);
  assert.ok(!block.includes('<RAILS_SERVER_ERRORS>'));
});

test('the server backtrace rides along', () => {
  const { attach } = harness();
  attach.record(aServerError());
  assert.match(attach.buildMessageBlock(), /app\/views\/posts\/show\.html\.erb:3/);
});

test('server errors are consumed by a send like any other', () => {
  const { attach } = harness();
  attach.record(aServerError());
  attach.clear();
  assert.equal(attach.errors.length, 0);
});

test('server error text is escaped in the popup', () => {
  const { attach, body } = harness();
  attach.record(aServerError({ message: 'NoMethodError: <img src=x onerror=alert(1)>' }));
  attach.openDetails();

  const html = body.children[body.children.length - 1].innerHTML;
  assert.ok(!html.includes('<img src=x'));
  assert.match(html, /&lt;img src=x/);
});

test('a server error reads as a server problem, not a page problem', () => {
  assert.match(friendlySummary(aServerError()), /server/i);
});

test('common Rails failures get their own plain sentence', () => {
  const say = (message) => friendlySummary(aServerError({ message }));

  assert.match(say('ActiveRecord::RecordNotFound: Couldn\'t find Post'), /couldn't find|doesn't exist/i);
  assert.match(say('ActiveRecord::StatementInvalid: PG::UndefinedColumn'), /database/i);
  assert.match(say('ActionView::Template::Error: undefined method'), /page|render/i);
  assert.match(say('ActiveRecord::PendingMigrationError'), /database/i);
});

test('an unrecognised server error still says something useful', () => {
  assert.match(friendlySummary(aServerError({ message: 'Whatever::Error: hmm' })), /server/i);
});

// ---------------------------------------------------------------------------
// Telling the two kinds apart
//
// The tray carries both browser errors (pushed from the preview over
// postMessage) and Rails crashes (polled from the server's own error feed).
// Until now it rendered "3 errors detected" for either, which hides the single
// most useful fact: a JavaScript error means the page loaded and something
// misbehaved, a Rails error means the request never made it out of the server —
// and the server ones happen even when the screen looks perfectly fine.
// ---------------------------------------------------------------------------

function trayWith(entries) {
  const tray = new ErrorAttach({ getAllowedOrigin: () => RAILS_ORIGIN });
  const banner = new FakeElement();
  tray.init(banner, new FakeElement());
  entries.forEach((e) => tray.record(e));
  return { tray, banner };
}

const jsError = (message = 'x is not a function') => ({
  id: `js-${message}`, kind: 'error', message, path: '/dash',
});
const railsError = (message = 'undefined method `titl?') => ({
  id: `rails-${message}`, kind: 'rails', message, path: '/posts/3',
});

test('banner names JavaScript when only browser errors are present', () => {
  const { banner } = trayWith([jsError()]);
  assert.match(banner.innerHTML, /1 JavaScript error detected/);
  assert.doesNotMatch(banner.innerHTML, /Rails/);
});

test('banner names Rails when only server errors are present', () => {
  const { banner } = trayWith([railsError()]);
  assert.match(banner.innerHTML, /1 Rails error detected/);
  assert.doesNotMatch(banner.innerHTML, /JavaScript/);
});

test('banner breaks the count down when both kinds are present', () => {
  const { banner } = trayWith([jsError(), railsError()]);
  assert.match(banner.innerHTML, /1 Rails/);
  assert.match(banner.innerHTML, /1 JavaScript/);
});

test('banner pluralises each kind independently', () => {
  const { banner } = trayWith([jsError('a'), jsError('b'), railsError('c')]);
  assert.match(banner.innerHTML, /1 Rails error/);
  assert.match(banner.innerHTML, /2 JavaScript errors/);
});

test('banner counts distinct problems, not occurrences', () => {
  // A crashing page raises the same error on every render; Rails counts those
  // in the dozens. "50 errors detected" would be alarming and wrong.
  const { tray, banner } = trayWith([railsError('same')]);
  tray.record({ ...railsError('same'), count: 40 });
  assert.match(banner.innerHTML, /1 Rails error detected/);
});

test('each popup entry is labelled with its kind', () => {
  const { tray } = trayWith([jsError(), railsError()]);
  const html = tray._detailsHtml();
  assert.match(html, /js-error-kind[^>]*>Rails</);
  assert.match(html, /js-error-kind[^>]*>JavaScript</);
});

test('a Rails entry is marked so it can be styled apart', () => {
  const { tray } = trayWith([railsError()]);
  assert.match(tray._detailsHtml(), /js-error-item--rails/);
});

test('the kind label cannot be injected from error text', () => {
  const { tray } = trayWith([{ id: 'x', kind: '<img src=x onerror=alert(1)>', message: 'hi' }]);
  const html = tray._detailsHtml();
  assert.doesNotMatch(html, /<img src=x/);
});
