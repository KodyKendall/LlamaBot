// A chat whose saved messages are gone must not open as a blank new chat (0.7.14).
//
// A mothership backup truncated the checkpoint tables on live boxes but left the
// sidebar's thread list alone. Every chat then opened as "Hi! What are we building
// today?" with no error, and a paying customer concluded the app was broken.
// /chat-history now marks that case with history_missing; the chat must say so.

import assert from 'node:assert/strict';
import test from 'node:test';

function el(tag) {
  return { tagName: tag, className: '', textContent: '', children: [], appendChild(c) { this.children.push(c); } };
}

globalThis.window = { dispatchEvent() {} };
globalThis.document = { createElement: el, querySelector: () => null };

const { ThreadManager } = await import('../../frontend/chat/threads/ThreadManager.js');

function managerWithHistory() {
  const history = el('div');
  const renderer = { clearMessages() {}, getMessageHistory: () => history };
  return { manager: new ThreadManager(renderer, null, null), history };
}

const GREETING = 'Hi! What are we building today?';

test('a thread whose history is gone says so instead of greeting', () => {
  const { manager, history } = managerWithHistory();
  manager.loadThreadMessages([
    { messages: [], history_missing: true, title: 'Quarterly analysis', message_count: 12 },
  ]);

  const text = history.children.map((c) => c.textContent).join(' ');
  assert.ok(!text.includes(GREETING), `greeting shown for a lost chat: ${text}`);
  assert.match(text, /no longer on this server/);
  assert.match(text, /start a new chat/i);
});

test('a genuinely new thread still gets the greeting', () => {
  const { manager, history } = managerWithHistory();
  manager.loadThreadMessages([{ messages: [] }]);

  assert.equal(history.children.length, 1);
  assert.equal(history.children[0].textContent, GREETING);
});
