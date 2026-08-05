// Regression test for SI#203: the "Update Now" button was dead on every box because
// `chat.html`'s update IIFE captured `overlay`/`pill` with `querySelector` at PARSE
// time, while the modal/pill markup only appears hundreds of lines further down the
// document. Both handles were `null` for the life of the page, so `openModal()`
// silently no-opped — no error, no telemetry, no modal.
//
// The property under test is ORDERING, so the harness deliberately runs the real
// script source against an EMPTY document and only registers the markup afterwards.
// A test that builds the DOM first would pass on the broken code and prove nothing.

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const CHAT_HTML = resolve(APP_ROOT, 'frontend', 'chat.html');

/** Pull the self-contained update-flow IIFE out of chat.html. */
function extractUpdateIife() {
  const html = readFileSync(CHAT_HTML, 'utf8');
  const start = html.indexOf('(function() {', html.indexOf("qs('update-modal')") - 4000);
  assert.ok(start > 0, 'could not locate the update IIFE in chat.html');
  const end = html.indexOf('})();', html.indexOf('update-modal-dismiss-error', start));
  assert.ok(end > start, 'could not locate the end of the update IIFE');
  return html.slice(start, end + '})();'.length);
}

class FakeClassList {
  constructor(initial = []) {
    this._set = new Set(initial);
  }
  add(...names) { names.forEach((n) => this._set.add(n)); }
  remove(...names) { names.forEach((n) => this._set.delete(n)); }
  contains(name) { return this._set.has(name); }
  toggle(name, force) {
    const on = force === undefined ? !this._set.has(name) : !!force;
    if (on) this._set.add(name); else this._set.delete(name);
    return on;
  }
}

class FakeElement {
  constructor(name, classes = []) {
    this.name = name;
    this.classList = new FakeClassList(classes);
    this.textContent = '';
  }
}

/**
 * Minimal DOM stub. `elements` starts empty so the script's parse-time lookups see
 * exactly what a real browser sees at line 557: nothing below has been parsed yet.
 */
function makeHarness() {
  const elements = new Map();
  const clickHandlers = [];

  const selectorName = (selector) => {
    const m = /\[data-llamabot="([^"]+)"\]/.exec(selector);
    return m ? m[1] : null;
  };

  const document = {
    querySelector(selector) {
      const name = selectorName(selector);
      return (name && elements.get(name)) || null;
    },
    addEventListener(type, fn) {
      if (type === 'click') clickHandlers.push(fn);
    },
  };

  const window = {
    __llamabotUpdateData: null,
    location: { reload() {} },
    addEventListener() {},
  };

  return { elements, clickHandlers, document, window };
}

/** A click event whose target matches exactly one `data-llamabot` affordance. */
function clickEventFor(name) {
  return {
    target: {
      closest(selector) {
        const m = /\[data-llamabot="([^"]+)"\]/.exec(selector);
        return m && m[1] === name ? { name } : null;
      },
    },
  };
}

test('Update Now opens the modal even though its markup parses after the script', () => {
  const h = makeHarness();

  // 1. Run the real script against an EMPTY document — this is parse time.
  const run = new Function(
    'document', 'window', 'fetch', 'setTimeout', 'console', 'location',
    extractUpdateIife(),
  );
  run(
    h.document,
    h.window,
    () => new Promise(() => {}),   // fetch: never resolves; we only test the open path
    () => 0,                       // setTimeout: no timers needed
    { log() {}, warn() {}, error() {} },
    h.window.location,
  );

  assert.equal(h.clickHandlers.length, 1, 'the IIFE should register one click handler');

  // 2. NOW the rest of the document parses and the markup exists.
  h.elements.set('update-modal', new FakeElement('update-modal', ['hidden']));
  h.elements.set('update-modal-confirm', new FakeElement('update-modal-confirm', ['hidden']));
  h.elements.set('update-modal-progress', new FakeElement('update-modal-progress', ['hidden']));
  h.elements.set('update-modal-complete', new FakeElement('update-modal-complete', ['hidden']));
  h.elements.set('update-modal-error', new FakeElement('update-modal-error', ['hidden']));
  h.elements.set('update-modal-notes', new FakeElement('update-modal-notes'));
  h.elements.set('update-pill', new FakeElement('update-pill', ['hidden']));
  h.elements.set('update-pill-text', new FakeElement('update-pill-text'));

  // 3. The update payload has arrived, so the guard in the handler passes.
  h.window.__llamabotUpdateData = {
    latest_versions: { llamabot: { version: '0.6.0f', notes: 'stuff' } },
  };

  // 4. The user clicks "Update Now".
  h.clickHandlers[0](clickEventFor('update-now-btn'));

  const overlay = h.elements.get('update-modal');
  assert.equal(
    overlay.classList.contains('hidden'),
    false,
    'openModal() no-opped: the overlay handle was captured before its markup parsed',
  );
  assert.equal(
    h.elements.get('update-modal-confirm').classList.contains('hidden'),
    false,
    'the confirm panel should be the visible state',
  );
});

test('the corner pill is reachable after the markup parses', () => {
  const h = makeHarness();
  const run = new Function(
    'document', 'window', 'fetch', 'setTimeout', 'console', 'location',
    extractUpdateIife(),
  );
  run(
    h.document,
    h.window,
    () => new Promise(() => {}),
    () => 0,
    { log() {}, warn() {}, error() {} },
    h.window.location,
  );

  h.elements.set('update-modal', new FakeElement('update-modal', ['hidden']));
  h.elements.set('update-modal-error', new FakeElement('update-modal-error', ['hidden']));
  h.elements.set('update-pill', new FakeElement('update-pill', ['hidden']));
  h.elements.set('update-pill-text', new FakeElement('update-pill-text'));

  // Dismissing a timeout notice calls setPill() — it must find the late-parsed pill.
  h.clickHandlers[0](clickEventFor('update-modal-dismiss-error'));

  const pill = h.elements.get('update-pill');
  assert.equal(pill.classList.contains('hidden'), false, 'setPill() could not reach the pill');
  assert.equal(h.elements.get('update-pill-text').textContent, 'Update may need a refresh');
});
