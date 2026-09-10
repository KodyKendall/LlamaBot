// The click-to-select tool must survive an app-preview reload.
//
// Reported from the CRM box (2026-09-10): the button lights up but nothing
// highlights, and it takes three clicks to recover. Two everyday sequences cause
// it, and both have the same shape — the parent posts `enable-element-selector`
// into the iframe exactly once and never re-checks that the iframe agreed:
//
//   1. Click the tool while the iframe is still loading. The Rails page's
//      message listener isn't registered yet (~4s on crm-4), so the message is
//      dropped on the floor. Button on, iframe off.
//   2. Turn the tool on, then the agent finishes an edit and the frame reloads.
//      The new document starts with selection mode off; the parent still thinks
//      it is on.
//
// Either way the user's next click runs disableSelectionMode() — the parent
// thinks it's on — so the tool only comes back on the third click.
//
// The fix re-sends the enable on every iframe `load`. `load` is observable
// cross-origin from the parent and fires after the document's module scripts
// have run, so the listener exists by then.

import assert from 'node:assert/strict';
import test from 'node:test';

class FakeClassList {
  constructor() { this._set = new Set(); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
}

/**
 * Minimal element: listeners, classList, dataset, attributes, and just enough
 * tree/child API for renderBadges() to run when an element is picked.
 */
class FakeElement {
  constructor() {
    this.classList = new FakeClassList();
    this.dataset = {};
    this.attributes = {};
    this.handlers = {};
    this.children = [];
    this.parentElement = null;
    this.isConnected = true;
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
    this.title = '';
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); }
  dispatch(type, evt = {}) { (this.handlers[type] || []).forEach((fn) => fn(evt)); }
  dispatchEvent(evt) { this.dispatch(evt && evt.type, evt); return true; }
  click() { this.dispatch('click', {}); }
  focus() {}
  appendChild(child) { this.children.push(child); child.parentElement = this; return child; }
  insertBefore(child) { this.children.unshift(child); child.parentElement = this; return child; }
  remove() {
    if (!this.parentElement) return;
    this.parentElement.children = this.parentElement.children.filter((c) => c !== this);
    this.parentElement = null;
    this.isConnected = false;
  }
}

/**
 * A stand-in for the live-site iframe. `posted` records every postMessage the
 * parent sends into it; `reload()` is what the real frame does after an agent
 * edit or a header navigation.
 */
function makeFrame({ hasContentWindow = true } = {}) {
  const frame = new FakeElement();
  frame.posted = [];
  frame.contentWindow = hasContentWindow
    ? { postMessage: (msg) => frame.posted.push(msg) }
    : null;
  frame.reload = () => frame.dispatch('load');
  return frame;
}

function makeEnv({ frame = makeFrame() } = {}) {
  const button = new FakeElement();
  const messageInput = new FakeElement();

  // renderBadges() inserts the badge strip just before the message input.
  const inputParent = new FakeElement();
  inputParent.appendChild(messageInput);

  globalThis.window = { addEventListener() {}, removeEventListener() {} };
  globalThis.document = { createElement: () => new FakeElement() };
  globalThis.Event = class { constructor(type, opts = {}) { this.type = type; Object.assign(this, opts); } };

  return { frame, button, messageInput, iframeManager: { liveSiteFrame: frame } };
}

const { ElementSelector } = await import('../../frontend/chat/ui/ElementSelector.js');

function enables(frame) {
  return frame.posted.filter((m) => m && m.type === 'enable-element-selector');
}

function build(env) {
  const selector = new ElementSelector(env.iframeManager);
  selector.init(env.button, env.messageInput);
  return selector;
}

// ---------------------------------------------------------------------------

test('an iframe reload while selection mode is on re-arms the iframe', () => {
  const env = makeEnv();
  const selector = build(env);

  selector.enableSelectionMode();
  assert.equal(enables(env.frame).length, 1, 'the initial enable should be sent');

  // The agent finishes an edit and LlamaBot reloads the preview.
  env.frame.reload();

  assert.equal(enables(env.frame).length, 2,
    'the fresh document has no idea selection mode is on — re-send the enable');
  assert.ok(env.button.classList.contains('active'),
    'the button should still read as on, because now it truthfully is');
  assert.equal(selector.isSelectionMode, true);
});

test('a click during iframe load is recovered when the load finishes', () => {
  const env = makeEnv();
  const selector = build(env);

  // Clicked while the Rails page's message listener does not exist yet: the
  // parent posts into a document that drops it.
  env.button.click();
  assert.equal(selector.isSelectionMode, true);
  assert.equal(enables(env.frame).length, 1);

  // The document finishes loading and registers its listener.
  env.frame.reload();

  assert.equal(enables(env.frame).length, 2,
    'the dropped enable should be re-sent once the iframe is actually ready');
});

test('with selection mode off, an iframe reload posts nothing', () => {
  const env = makeEnv();
  build(env);

  env.frame.reload();

  assert.equal(env.frame.posted.length, 0,
    're-arming a tool the user never turned on would enable it behind their back');
});

test('turning the tool off stops the re-arming', () => {
  const env = makeEnv();
  const selector = build(env);

  selector.enableSelectionMode();
  selector.disableSelectionMode();
  const before = enables(env.frame).length;

  env.frame.reload();

  assert.equal(enables(env.frame).length, before,
    'no enable should follow a reload once the user has switched the tool off');
});

test('picking an element ends selection mode, so a later reload stays quiet', () => {
  const env = makeEnv();
  const selector = build(env);

  selector.enableSelectionMode();
  // The iframe reports a pick; handleElementSelected() disables the mode.
  selector.handleElementSelected('Save button', '<button>Save</button>');
  const before = enables(env.frame).length;

  env.frame.reload();

  assert.equal(enables(env.frame).length, before);
  assert.equal(selector.isSelectionMode, false);
});

test('a frame with no contentWindow does not throw on load', () => {
  const env = makeEnv({ frame: makeFrame({ hasContentWindow: false }) });
  const selector = build(env);

  selector.isSelectionMode = true;

  // A frame mid-navigation can have a null contentWindow; the re-arm must not
  // take the whole chat UI down with it.
  assert.doesNotThrow(() => env.frame.reload());
});
