// Discard can wipe a never-committed app (0.7.15, LP-7).
//
// A customer who never made a checkpoint clicked Discard on "430 unsaved
// changes" — his whole app — after the same one-line confirm() that a single
// edited file gets. The app went to a 404, and the only Undo was inside the
// checkpoint panel he never reopened. He emailed support and waited 10 hours.

import assert from 'node:assert/strict';
import test from 'node:test';

// ---------------------------------------------------------------- tiny DOM

function el(tag = 'div') {
  const node = {
    tagName: tag.toUpperCase(), className: '', textContent: '', style: {}, children: [], dataset: {},
    disabled: false, onclick: null, parent: null, _html: '',
    set innerHTML(v) { this._html = v; this.children = []; },
    get innerHTML() { return this._html; },
    appendChild(c) { c.parent = this; this.children.push(c); return c; },
    insertBefore(c, ref) {
      c.parent = this;
      const i = this.children.indexOf(ref);
      this.children.splice(i < 0 ? this.children.length : i, 0, c);
      return c;
    },
    remove() { if (this.parent) this.parent.children = this.parent.children.filter((x) => x !== this); this.parent = null; },
    querySelector(sel) { return find(this, sel); },
    get isConnected() { return Boolean(this.parent); },
  };
  return node;
}

function find(root, sel) {
  const cls = sel.replace(/^\./, '');
  for (const c of root.children) {
    if (c.className.split(' ').includes(cls)) return c;
    const deep = find(c, sel);
    if (deep) return deep;
  }
  return null;
}

const text = (node) => [node.textContent, ...node.children.map(text)].join(' ');

let chatSection;
let inputArea;
globalThis.window = { dispatchEvent() {} };
globalThis.document = {
  createElement: el,
  querySelector: (sel) => (sel === '.chat-section' ? chatSection : sel === '.input-area' ? inputArea : null),
};

const { needsStrongWarning, discardWarningText } = await import('../../frontend/chat/checkpoints/discardWarning.js');
const { DiscardUndoCard } = await import('../../frontend/chat/checkpoints/DiscardUndoCard.js');
const { CheckpointManager } = await import('../../frontend/chat/checkpoints/CheckpointManager.js');

const LEO_NOTE = {
  has_changes: true, total_count: 430, controller_count: 22, model_count: 26, view_count: 180,
  untracked_controllers: true, untracked_models: true,
};
const ONE_EDIT = {
  has_changes: true, total_count: 1, controller_count: 0, model_count: 1, view_count: 0,
  untracked_controllers: false, untracked_models: false,
};

// ---------------------------------------------------------------- the rule

test('a whole never-committed app gets the strong warning', () => {
  assert.equal(needsStrongWarning(LEO_NOTE), true);
});

test('one edited, committed file keeps the light confirm', () => {
  assert.equal(needsStrongWarning(ONE_EDIT), false);
});

test('50 files, or any never-committed controller or model, is enough', () => {
  assert.equal(needsStrongWarning({ ...ONE_EDIT, total_count: 50 }), true);
  assert.equal(needsStrongWarning({ ...ONE_EDIT, total_count: 49 }), false);
  assert.equal(needsStrongWarning({ ...ONE_EDIT, untracked_controllers: true }), true);
  assert.equal(needsStrongWarning({ ...ONE_EDIT, untracked_models: true }), true);
});

test('the warning names the size in plain words', () => {
  assert.equal(
    discardWarningText(LEO_NOTE),
    'This will remove 430 files, including 22 controllers and 26 models. '
      + 'Your app will stop working until you undo.',
  );
  assert.equal(
    discardWarningText({ ...ONE_EDIT, total_count: 3, controller_count: 1, model_count: 0 }),
    'This will remove 3 files, including 1 controller. Your app will stop working until you undo.',
  );
  assert.equal(
    discardWarningText({ ...ONE_EDIT, total_count: 60, model_count: 0 }),
    'This will remove 60 files. Your app will stop working until you undo.',
  );
});

// ---------------------------------------------------------------- the discard flow

function manager(changes, choice) {
  const m = Object.create(CheckpointManager.prototype);
  const calls = { posts: [], confirms: 0, form: 0, panel: 0 };
  m.chatApp = {};
  m.isVisible = false;
  m.togglePanel = () => { calls.panel += 1; m.isVisible = !m.isVisible; };
  m.showSaveCheckpointForm = () => { calls.form += 1; };
  m.showSuccess = () => {};
  m.showError = (msg) => { throw new Error(msg); };
  m.checkUncommittedChanges = () => {};
  m.updateUnsavedBadge = () => {};
  m.refreshDiscardUndo = () => {};
  m.confirmLargeDiscard = async (c) => { calls.modal = c; return choice; };
  globalThis.confirm = () => { calls.confirms += 1; return true; };
  globalThis.fetch = async (url, opts = {}) => {
    if (opts.method === 'POST') calls.posts.push(url);
    const body = url === '/api/checkpoints/uncommitted' ? changes : { success: true, message: 'ok', backup_ref: 'r' };
    return { ok: true, json: async () => body };
  };
  return { m, calls };
}

test('Scenario 1: the big modal, and "Save a checkpoint instead" discards nothing', async () => {
  const { m, calls } = manager(LEO_NOTE, 'checkpoint');
  await m.discardUncommittedChanges();

  assert.equal(calls.confirms, 0, 'fell back to window.confirm');
  assert.equal(calls.modal.total_count, 430);
  assert.deepEqual(calls.posts, []);
  assert.equal(calls.form, 1, 'checkpoint form not opened');
});

test('Scenario 1: choosing the secondary Discard still discards (snapshot first)', async () => {
  const { m, calls } = manager(LEO_NOTE, 'discard');
  await m.discardUncommittedChanges();
  assert.deepEqual(calls.posts, ['/api/checkpoints/discard']);
});

test('Scenario 1: closing the modal discards nothing', async () => {
  const { m, calls } = manager(LEO_NOTE, 'cancel');
  await m.discardUncommittedChanges();
  assert.deepEqual(calls.posts, []);
});

test('Scenario 2: one file uses the light confirm', async () => {
  const { m, calls } = manager(ONE_EDIT, 'cancel');
  await m.discardUncommittedChanges();
  assert.equal(calls.confirms, 1);
  assert.equal(calls.modal, undefined);
  assert.deepEqual(calls.posts, ['/api/checkpoints/discard']);
});

// ---------------------------------------------------------------- the chat card

function freshChat() {
  chatSection = el();
  inputArea = el();
  inputArea.className = 'input-area';
  chatSection.appendChild(el());       // message history
  chatSection.appendChild(inputArea);
}

const backup = (pending, ref = 'refs/llamabot/discards/20261007-abc') =>
  ({ ref, sha: 'abc', created_at: '2026-10-07T07:26:13Z', file_count: 430, pending });

test('Scenario 3: a pending discard shows an Undo card above the chat input', () => {
  freshChat();
  const card = new DiscardUndoCard({ onUndo() {}, onDismiss() {} });
  card.render([backup(false, 'refs/llamabot/discards/new'), backup(true)]);

  const node = chatSection.children[1];
  assert.ok(node.className.includes('discard-undo-card'), 'card not placed before the input');
  assert.match(text(node), /430 files/);
  assert.match(text(node), /Undo discard/);
});

test('no pending discard, no card (and an old one goes away)', () => {
  freshChat();
  const card = new DiscardUndoCard({ onUndo() {}, onDismiss() {} });
  card.render([backup(true)]);
  card.render([backup(false)]);
  assert.equal(find(chatSection, '.discard-undo-card'), null);
});

test('the card undoes and dismisses that exact backup', () => {
  freshChat();
  const seen = [];
  const card = new DiscardUndoCard({ onUndo: (ref) => seen.push(['undo', ref]), onDismiss: (ref) => seen.push(['dismiss', ref]) });
  card.render([backup(true)]);

  find(chatSection, '.discard-undo-card-undo').onclick();
  find(chatSection, '.discard-undo-card-dismiss').onclick();
  assert.deepEqual(seen, [
    ['undo', 'refs/llamabot/discards/20261007-abc'],
    ['dismiss', 'refs/llamabot/discards/20261007-abc'],
  ]);
});

test('the manager checks for a pending discard on page load, so the card survives a reload', () => {
  const src = CheckpointManager.toString();
  assert.match(src, /new DiscardUndoCard\(/);
  assert.match(src, /this\.undoCard\.render\(data\.backups/);
});

test('undo from the card posts its backup ref; dismiss hits the dismiss endpoint', async () => {
  const { m, calls } = manager(LEO_NOTE, 'cancel');
  m.refreshDiscardUndo = () => {};
  const bodies = [];
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => { if (opts.body) bodies.push([url, JSON.parse(opts.body)]); return realFetch(url, opts); };

  await m.undoDiscard('refs/llamabot/discards/x');
  await m.dismissDiscard('refs/llamabot/discards/x');

  assert.deepEqual(bodies, [
    ['/api/checkpoints/discard/undo', { backup_ref: 'refs/llamabot/discards/x' }],
    ['/api/checkpoints/discard/dismiss', { backup_ref: 'refs/llamabot/discards/x' }],
  ]);
});

test('the page-load check renders the card even before the panel exists', async () => {
  // It used to run inside createCheckpointPanel(), before this.checkpointPanel
  // was assigned, so it returned early and nothing showed after a reload.
  const m = Object.create(CheckpointManager.prototype);
  const rendered = [];
  m.checkpointPanel = undefined;
  m.undoCard = { render: (b) => rendered.push(b) };
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ has_backups: true, backups: [backup(true)] }) });

  await m.refreshDiscardUndo();
  assert.equal(rendered.length, 1);
});

test('initializeUI checks for a pending discard after the panel is built', () => {
  const src = CheckpointManager.prototype.initializeUI.toString();
  assert.ok(src.indexOf('this.refreshDiscardUndo()') > src.indexOf('this.checkpointPanel = '), src);
});
