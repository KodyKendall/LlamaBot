import assert from 'node:assert/strict';
import test from 'node:test';

import { sendButtonAction } from '../../frontend/chat/utils/sendButtonMode.js';

test('while Leo works, typed text sends (queues); an empty box makes the button Stop', () => {
  assert.equal(sendButtonAction({ running: true, hasText: true }), 'send');
  assert.equal(sendButtonAction({ running: true, hasText: false }), 'stop');
  assert.equal(sendButtonAction({ running: false, hasText: true }), 'send');
  assert.equal(sendButtonAction({ running: false, hasText: false }), 'send');
});

test('background research alone makes an empty box show Stop (the only way to cancel it)', () => {
  assert.equal(sendButtonAction({ running: false, backgroundRunning: true, hasText: false }), 'stop');
  assert.equal(sendButtonAction({ running: false, backgroundRunning: true, hasText: true }), 'send');
});
