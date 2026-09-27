// A blocked customer can always keep going on their own ChatGPT account (0.7.11).
// The card offers that way out only when the box's policy allows a ChatGPT model;
// offering a button that cannot work would be a fresh dead end.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  USE_CHATGPT_LABEL,
  paywallOffersChatgpt,
  pickChatgptModel,
} from '../../frontend/chat/messages/paywallCopy.js';

test('the card offers ChatGPT only when the box allows it', () => {
  assert.equal(paywallOffersChatgpt({ chatgpt_available: true }), true);
  assert.equal(paywallOffersChatgpt({ chatgpt_available: false }), false);
  // An older box sends no flag: no button, exactly today's card.
  assert.equal(paywallOffersChatgpt({}), false);
  assert.equal(paywallOffersChatgpt(undefined), false);
});

test('the button says what it does and never names the limit', () => {
  assert.equal(USE_CHATGPT_LABEL, 'Use your ChatGPT account');
});

test('the switch lands on the first ChatGPT model the picker allows', () => {
  const options = [
    { value: 'deepseek-v4-flash', disabled: false },
    { value: 'gpt-6-luna-chatgpt', disabled: true },
    { value: 'gpt-6-sol-chatgpt', disabled: false },
  ];
  assert.equal(pickChatgptModel(options), 'gpt-6-sol-chatgpt');
  assert.equal(pickChatgptModel([{ value: 'deepseek-v4-flash', disabled: false }]), null);
});
