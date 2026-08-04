// When the operator disables vision, an image must be refused at ATTACH time.
// Letting it attach and only blocking the send leaves a useless thumbnail sitting
// in the composer (QA: "should it not allow the image to attach, since it is
// useless to still have it attach if the AI can't read it").
//
// Non-image attachments (PDFs, docs) are unaffected — the gate is images only.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const { FileAttachmentManager } = await import(
  resolve(APP_ROOT, 'frontend', 'chat', 'ui', 'FileAttachmentManager.js')
);

/** Minimal File stand-in: handleFileSelect only reads .name/.type/.size. */
const fakeFile = (name, type, size = 1024) => ({ name, type, size });

/**
 * A manager with no DOM: previewContainer stays null so renderPreview() bails
 * out early, and encodeFile is stubbed so no FileReader is needed.
 */
function makeManager() {
  const manager = new FileAttachmentManager();
  manager.fileInput = { value: '' };
  manager.encodeFile = async () => 'BASE64';
  return manager;
}

test('images are refused while vision is disabled', async () => {
  const manager = makeManager();
  manager.blockImages = true;
  let blocked = 0;
  manager.onImageBlocked = () => { blocked += 1; };

  await manager.handleFileSelect([fakeFile('screenshot.png', 'image/png')]);

  assert.equal(manager.attachments.length, 0, 'the image must not attach');
  assert.equal(blocked, 1, 'the UI must be told why nothing attached');
});

test('non-image files still attach while vision is disabled', async () => {
  const manager = makeManager();
  manager.blockImages = true;
  let blocked = 0;
  manager.onImageBlocked = () => { blocked += 1; };

  await manager.handleFileSelect([fakeFile('report.pdf', 'application/pdf')]);

  assert.equal(manager.attachments.length, 1);
  assert.equal(manager.attachments[0].filename, 'report.pdf');
  assert.equal(blocked, 0);
});

test('images attach normally when vision is allowed', async () => {
  const manager = makeManager();
  let blocked = 0;
  manager.onImageBlocked = () => { blocked += 1; };

  await manager.handleFileSelect([fakeFile('screenshot.png', 'image/png')]);

  assert.equal(manager.attachments.length, 1);
  assert.equal(blocked, 0);
});

test('a mixed selection keeps the documents and drops the images', async () => {
  const manager = makeManager();
  manager.blockImages = true;
  let blocked = 0;
  manager.onImageBlocked = () => { blocked += 1; };

  await manager.handleFileSelect([
    fakeFile('a.png', 'image/png'),
    fakeFile('b.pdf', 'application/pdf'),
    fakeFile('c.jpg', 'image/jpeg'),
  ]);

  assert.deepEqual(manager.attachments.map(a => a.filename), ['b.pdf']);
  assert.equal(blocked, 1, 'one notice per selection, not one per image');
});
