// LP-44: both attach buttons are images to the user, and a text-only model gets a
// plain notice instead of a silent drop (or a model switch behind the user's back).

import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  isImageAttachment, hasAnyImage, visionCapableModels, cannotSeeImagesNotice,
} from '../../frontend/chat/utils/imageNotice.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (...p) => readFileSync(resolve(APP, ...p), 'utf8');
const INDEX = read('frontend', 'chat', 'index.js');
const CHAT_HTML = read('frontend', 'chat.html');
const THREADS = read('frontend', 'chat', 'threads', 'ThreadManager.js');

const paperclip = { filename: 'a.png', mime_type: 'image/png', data: 'xx' };
const uploaded = { type: 'uploaded_file', filename: 'h.png', path: 'app/assets/images/h.png', mime_type: 'image/png' };
const uploadedNoMime = { type: 'uploaded_file', filename: 'h.JPG', path: 'app/assets/images/h.JPG' };
const uploadedPdf = { type: 'uploaded_file', filename: 'r.pdf', path: 'app/imports/r.pdf', mime_type: 'application/pdf' };

test('both attach paths count as images', () => {
  assert.equal(isImageAttachment(paperclip), true);
  assert.equal(isImageAttachment(uploaded), true);
  assert.equal(isImageAttachment(uploadedNoMime), true);
  assert.equal(isImageAttachment(uploadedPdf), false);
  assert.equal(hasAnyImage([uploadedPdf, uploaded]), true);
  assert.equal(hasAnyImage([uploadedPdf]), false);
});

const opt = (value, label, disabled = false) => ({ value, disabled, textContent: label, dataset: { shortLabel: label } });

test('the notice names the enabled models that can see images', () => {
  const options = [opt('deepseek-v4-flash', 'DeepSeek V4 Flash'), opt('gpt-6-luna', 'Luna'),
    opt('muse', 'Muse 1.3'), opt('gemini', 'Gemini', true)];
  const can = (m) => m !== 'deepseek-v4-flash';
  const labels = visionCapableModels(options, can);
  assert.deepEqual(labels, ['Luna', 'Muse 1.3']);
  assert.equal(
    cannotSeeImagesNotice('DeepSeek V4 Flash', labels),
    "DeepSeek V4 Flash can't see images, so Leo won't see the picture you attached. "
      + 'Models here that can: Luna, Muse 1.3. Switch models to have Leo look at it.',
  );
  assert.match(cannotSeeImagesNotice('X', []), /No model on this workspace can see images/);
});

test('no automatic model switch or new thread for an image any more', () => {
  assert.doesNotMatch(INDEX, /createNewThread\(\{ isImageSwitch: true \}\)/);
  assert.doesNotMatch(INDEX, /I switched to \$\{this\.modelLabel\(imageModel\)\}/);
});

test('at send, a text-only model gets the notice in the chat, announced, and the message still sends', () => {
  assert.match(INDEX, /const imageNotSeen = hasAnyImage\(attachments\) && !this\.modelSupportsImages\(llmModel\)/);
  assert.match(INDEX, /showCannotSeeImagesNotice\(llmModel\)/);
  const method = INDEX.slice(INDEX.indexOf('  showCannotSeeImagesNotice('));
  assert.match(method.slice(0, 900), /addMessage\(text, 'system_message'\)/);
  assert.match(method.slice(0, 900), /LeoA11y\?\.announce\(text\)/);
});

test('Uploaded-files images are not blocked when vision is off (Leo still needs the path)', () => {
  const gate = INDEX.slice(INDEX.indexOf('  hasImageAttachment('), INDEX.indexOf('  hasImageAttachment(') + 400);
  assert.match(gate, /a\.type !== 'uploaded_file'/);
});

test('the pre-send banner is a status region and no longer only mid-conversation', () => {
  assert.match(CHAT_HTML, /data-llamabot="image-switch-banner" role="status"/);
  const fn = INDEX.slice(INDEX.indexOf('  updateImageSwitchBanner()'));
  const body = fn.slice(0, fn.indexOf('\n  }\n'));
  assert.doesNotMatch(body.slice(body.indexOf('image-switch-banner')), /conversationHasMessages/);
  assert.match(body, /hasAnyImage\(attachments\)/);
});

test('the picture view_image adds is hidden from the chat history', () => {
  assert.match(THREADS, /additional_kwargs\?\.view_image === true/);
});

test('an Uploaded-files image reaches the server in the frame (path only), other uploads stay text', () => {
  // Found taking the LP-44 screenshots: the frame filtered out EVERY uploaded_file,
  // so the server never saw the attachment and the image still never reached the model.
  const frame = INDEX.slice(INDEX.indexOf('const messageData = {'), INDEX.indexOf('const messageData = {') + 1200);
  assert.match(frame, /attachments: attachments\.filter\(a => a\.type !== 'uploaded_file' \|\| isImageAttachment\(a\)\)/);
});

test('the notice lands after the user bubble, and the banner clears once the composer is empty', () => {
  // Found taking the LP-44 screenshots: the notice was added before the user's bubble
  // (scrolled out of view) and the pre-send banner stayed up after the send.
  const send = INDEX.slice(INDEX.indexOf("this.messageRenderer.addMessage(input.value.trim(), 'human'"));
  const afterClear = send.slice(send.indexOf('this.fileAttachmentManager.clearAttachments();'));
  assert.match(afterClear.slice(0, 600), /if \(imageNotSeen\) this\.showCannotSeeImagesNotice\(llmModel\);/);
  assert.match(afterClear.slice(0, 600), /this\.updateImageSwitchBanner\(\);/);
});
