// "My Uploads" must open the full Asset Library modal directly.
//
// It used to open a small intermediate panel — the same list of filenames with
// no previews — from which you clicked an expand button to reach the real
// library. Two clicks to get anywhere useful; the panel is skipped now.
//
// Also guards the spreadsheet preview: it must render a cell grid from the
// server parse, never hand the file to Microsoft's Office Online viewer.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { FileAttachmentManager } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'ui', 'FileAttachmentManager.js')
);

/** Minimal element stand-in: records listeners and classList state. */
function fakeEl(initialClasses = ['hidden']) {
  const classes = new Set(initialClasses);
  return {
    handlers: {},
    attrs: {},
    innerHTML: '',
    addEventListener(evt, fn) { this.handlers[evt] = fn; },
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => { on ? classes.add(c) : classes.delete(c); },
    },
    contains: () => false,
    querySelectorAll: () => [],
    querySelector: () => null,
  };
}

/** initFileBrowser registers a document-level outside-click listener. */
function withFakeDocument(run) {
  const prev = globalThis.document;
  globalThis.document = { addEventListener() {} };
  try { return run(); } finally { globalThis.document = prev; }
}

test('clicking My Uploads opens the asset modal, not the small panel', () => {
  const manager = new FileAttachmentManager();
  const browseBtn = fakeEl();
  const panel = fakeEl();
  const list = fakeEl();

  let opened = 0;
  let toggledPanel = 0;
  manager.openAssetModal = () => { opened += 1; };
  manager.toggleFileBrowser = () => { toggledPanel += 1; };

  withFakeDocument(() => manager.initFileBrowser(browseBtn, panel, list, null));
  browseBtn.handlers.click({ stopPropagation() {} });

  assert.equal(opened, 1, 'the asset library modal must open');
  assert.equal(toggledPanel, 0, 'the intermediate file-browser panel must be skipped');
});

test('the attach menu closes when My Uploads opens the modal', () => {
  const manager = new FileAttachmentManager();
  const browseBtn = fakeEl();
  const menu = fakeEl();
  manager.attachMenu = menu;
  manager.openAssetModal = () => {};

  withFakeDocument(() => manager.initFileBrowser(browseBtn, fakeEl(), fakeEl(), null));
  browseBtn.handlers.click({ stopPropagation() {} });

  assert.ok(menu.classList.contains('hidden'), 'the file-attach menu must close');
});

test('spreadsheets render from the server parse endpoint', async () => {
  const manager = new FileAttachmentManager();
  const stage = fakeEl();
  const requested = [];
  globalThis.fetch = async (url) => {
    requested.push(url);
    return {
      ok: true,
      status: 200,
      json: async () => ({ sheets: ['Data'], active: 0, rows: [['a', '1']], total_rows: 1, truncated: false }),
    };
  };

  let gridRows = null;
  manager.renderSheetGrid = (rows) => { gridRows = rows; };
  stage.querySelector = () => fakeEl();

  await manager.renderSpreadsheetPreview('app/imports/book.xlsx', '/api/uploaded-files/preview?path=x', stage);

  assert.equal(requested.length, 1);
  assert.ok(requested[0].startsWith('/api/uploaded-files/sheet?path='), `unexpected URL: ${requested[0]}`);
  assert.deepEqual(gridRows, [['a', '1']]);
});

test('a 415 from the server falls back to the client-side reader', async () => {
  const manager = new FileAttachmentManager();
  const stage = fakeEl();
  globalThis.fetch = async () => ({ ok: false, status: 415 });

  let fellBack = 0;
  manager.renderSpreadsheetPreviewClientSide = async () => { fellBack += 1; };

  await manager.renderSpreadsheetPreview('app/imports/old.xls', '/api/uploaded-files/preview?path=x', stage);

  assert.equal(fellBack, 1, 'legacy .xls must still render via SheetJS');
});


/** An asset modal wired up with just the pieces initAssetModal touches. */
function makeAssetModal() {
  const manager = new FileAttachmentManager();
  const expandBtn = fakeEl([]);
  const modal = fakeEl([]);
  const parts = {
    '[data-llamabot="asset-modal-expand"]': expandBtn,
    '[data-llamabot="asset-modal-list"]': fakeEl([]),
    '[data-llamabot="asset-modal-preview"]': fakeEl([]),
    '[data-llamabot="asset-modal-count"]': fakeEl([]),
  };
  modal.querySelector = (sel) => parts[sel] || null;

  withFakeDocument(() => manager.initAssetModal(modal, null));
  return { manager, modal, expandBtn };
}

test('the expand button full-screens the asset library and back', () => {
  const { manager, modal, expandBtn } = makeAssetModal();

  expandBtn.handlers.click();
  assert.ok(modal.classList.contains('asset-modal--expanded'), 'must go full screen');
  assert.equal(expandBtn.getAttribute('aria-pressed'), 'true');

  expandBtn.handlers.click();
  assert.ok(!modal.classList.contains('asset-modal--expanded'), 'must collapse again');
  assert.equal(expandBtn.getAttribute('aria-pressed'), 'false');
  assert.ok(manager.assetModalExpandBtn, 'the button stays wired');
});

test('closing the modal drops full screen so the file list is back next time', () => {
  const { manager, modal, expandBtn } = makeAssetModal();

  expandBtn.handlers.click();
  manager.hideAssetModal();

  assert.ok(modal.classList.contains('hidden'));
  assert.ok(!modal.classList.contains('asset-modal--expanded'));
});
