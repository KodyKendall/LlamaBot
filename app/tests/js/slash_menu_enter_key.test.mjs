// Enter, while the slash menu is open, belongs to the MENU — it picks the
// highlighted row and leaves the message in the box so you can keep typing (a
// second Enter sends). That is delicate: ChatApp registers its own Enter-to-send
// listener on the very same textarea, and it registers it EARLIER in boot. Two
// listeners on one element fire in registration order no matter what either one
// does, so the menu's listener has to sit on the document's capture phase to get
// first refusal. These tests model that ordering and pin it — a regression here
// sends a half-typed message to the agent.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { SlashCommandManager } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'ui', 'SlashCommandManager.js'));

const GUIDES = [
  {
    slug: 'pdf-download-export',
    title: 'PDF Download Export',
    category: 'Reports',
    summary: 'Render any page as a downloadable PDF.',
    tags: ['pdf', 'export'],
    url: 'https://llamapress.ai/cookbook/pdf-download-export',
  },
  {
    slug: 'inline-data-tables',
    title: 'Inline Data Tables',
    category: 'UI',
    summary: 'Sortable, filterable tables.',
    tags: ['tables'],
    url: 'https://llamapress.ai/cookbook/inline-data-tables',
  },
];

const MENTION = '@cookbook:pdf-download-export '
  + '(https://llamapress.ai/cookbook/pdf-download-export.json)';

/**
 * The parts of the DOM event model this bug lives in: listeners on the target
 * element run in registration order (the capture flag does NOT reorder them),
 * document-capture listeners run before all of them, and stopPropagation stops
 * the event at the end of the current node — not mid-node.
 */
function makeDom() {
  const docCapture = [];
  const docBubble = [];
  const onInput = [];

  const doc = {
    addEventListener(type, fn, capture = false) {
      if (type !== 'keydown') return;
      (capture ? docCapture : docBubble).push(fn);
    },
  };

  const input = {
    value: '',
    selectionStart: 0,
    ownerDocument: doc,
    addEventListener(type, fn) { if (type === 'keydown') onInput.push(fn); },
    contains() { return false; },
    focus() {},
    setSelectionRange(start) { this.selectionStart = start; },
    dispatchEvent() {},
  };

  /** Type text into the composer, caret at the end unless given. */
  function type(value, caret = null) {
    input.value = value;
    input.selectionStart = caret == null ? value.length : caret;
  }

  function press(key, { shiftKey = false } = {}) {
    let stopped = false;
    const event = {
      key,
      shiftKey,
      target: input,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { stopped = true; },
    };
    for (const node of [docCapture, onInput, docBubble]) {
      for (const fn of node) fn(event);      // whole node runs, then we check
      if (stopped) break;
    }
    return event;
  }

  return { doc, input, press, type, docCaptureCount: () => docCapture.length };
}

/**
 * Dropdown stand-in. The manager sets innerHTML, toggles classes, and walks the
 * rendered rows to move the highlight — so the fake counts the rows it was given
 * and hands back that many stubs.
 */
function fakeDropdown() {
  const classes = new Set(['hidden']);
  return {
    innerHTML: '',
    classList: {
      add(c) { classes.add(c); },
      remove(c) { classes.delete(c); },
      contains(c) { return classes.has(c); },
    },
    querySelectorAll(selector) {
      if (selector !== '.slash-command-item') return [];
      const rows = this.innerHTML.split('class="slash-command-item').length - 1;
      return Array.from({ length: rows }, () => ({
        classList: { toggle() {} },
        addEventListener() {},
        scrollIntoView() {},
      }));
    },
    contains() { return false; },
  };
}

/**
 * A manager wired to the fake DOM, with ChatApp's Enter-to-send listener already
 * on the textarea — registered FIRST, exactly like the real boot order.
 * `sent` counts messages that would have gone to the agent.
 */
function makeHarness() {
  const dom = makeDom();
  const sent = [];

  dom.input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (dom.input.value.trim().length > 0) sent.push(dom.input.value);
    }
  });

  const previousDocument = globalThis.document;
  globalThis.document = dom.doc;
  const manager = new SlashCommandManager({});
  manager.messageInput = dom.input;
  manager.dropdown = fakeDropdown();
  manager._esc = (t) => String(t == null ? '' : t);
  manager.cookbookGuides = GUIDES;
  manager.cookbookFetchedAt = 1;
  manager.attachEventListeners();
  globalThis.document = previousDocument;

  return { dom, manager, sent };
}

test('the menu listens on the document capture phase, ahead of the composer', () => {
  const { dom } = makeHarness();
  assert.equal(dom.docCaptureCount(), 1,
    'a listener on the textarea itself would fire after ChatApp\'s send');
});

test('Enter picks the recipe instead of sending the message', () => {
  const { dom, manager, sent } = makeHarness();

  dom.type('/cookbook pdf');
  manager.handleInput();
  assert.equal(manager.isOpen, true);

  const event = dom.press('Enter');

  assert.deepEqual(sent, [], 'nothing may reach the agent while the menu is open');
  assert.equal(event.defaultPrevented, true, 'no newline in the composer either');
  assert.equal(manager.isOpen, false, 'the menu closes once you have picked');
  assert.equal(dom.input.value, `${MENTION} `);
});

test('the pick leaves a trailing space, and a second Enter sends', () => {
  const { dom, manager, sent } = makeHarness();

  dom.type('/cookbook pdf');
  manager.handleInput();
  dom.press('Enter');

  assert.ok(dom.input.value.endsWith(') '), 'you can keep talking without a space key');

  dom.press('Enter');

  assert.deepEqual(sent, [`${MENTION} `], 'the second Enter is the send');
  assert.equal(dom.input.value, `${MENTION} `, 'sending must not alter the text');
});

test('Enter mid-sentence keeps the thought either side of the recipe', () => {
  const { dom, manager, sent } = makeHarness();

  const typed = 'add a pdf export /cookbook pdf and make it purple';
  dom.type(typed, typed.indexOf(' and make'));
  manager.handleInput();
  dom.press('Enter');

  assert.deepEqual(sent, []);
  assert.equal(dom.input.value, `add a pdf export ${MENTION} and make it purple`);
});

test('Enter with nothing to pick gets out of the way and sends', () => {
  // "No recipes match" — the menu has nothing to give, so swallowing Enter would
  // just look like a broken send key.
  const { dom, manager, sent } = makeHarness();

  dom.type('hello /cookbook zzzz');
  manager.handleInput();
  assert.equal(manager.isOpen, true);
  assert.equal(manager.selectedIndex, -1);

  dom.press('Enter');

  assert.deepEqual(sent, ['hello /cookbook zzzz']);
});

test('Shift+Enter is still a newline, not a pick', () => {
  const { dom, manager, sent } = makeHarness();

  dom.type('/cookbook pdf');
  manager.handleInput();
  const event = dom.press('Enter', { shiftKey: true });

  assert.equal(dom.input.value, '/cookbook pdf', 'nothing was inserted');
  assert.deepEqual(sent, []);
  assert.equal(event.defaultPrevented, false, 'the textarea gets its newline');
});

test('arrows and Escape drive the menu without touching the composer', () => {
  const { dom, manager, sent } = makeHarness();

  dom.type('/cookbook');
  manager.handleInput();
  assert.equal(manager.filteredCommands.length, 2);

  dom.press('ArrowDown');
  assert.equal(manager.selectedIndex, 1);
  dom.press('ArrowUp');
  assert.equal(manager.selectedIndex, 0);

  const escape = dom.press('Escape');
  assert.equal(manager.isOpen, false);
  assert.equal(escape.defaultPrevented, true);

  assert.deepEqual(sent, []);
  assert.equal(dom.input.value, '/cookbook', 'the menu never rewrites what you typed');
});

test('with the menu closed, Enter sends like it always did', () => {
  const { dom, sent } = makeHarness();

  dom.type('just a normal message');
  dom.press('Enter');

  assert.deepEqual(sent, ['just a normal message']);
});
