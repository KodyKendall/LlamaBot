import assert from 'node:assert/strict';
import test from 'node:test';

import { BackgroundTaskPill, pillText } from '../../frontend/chat/ui/BackgroundTaskPill.js';
import { MessageHandler } from '../../frontend/chat/websocket/MessageHandler.js';
import { isTaskNotification, taskNotificationMarker } from '../../frontend/chat/threads/ThreadManager.js';

function fakeElement() {
  const classes = new Set(['hidden']);
  return {
    textContent: '',
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
    },
  };
}

test('pill reads "Research running · M:SS" from when the research started', () => {
  const now = 1_000_000;
  const tasks = [{ label: 'Research', startedAt: (now - 100_000) / 1000 }];
  assert.equal(pillText(tasks, now), 'Research running · 1:40');
  assert.equal(pillText([], now), '');
});

test('pill tracks tasks per thread, survives other turns, clears when results land', () => {
  let current = 't1';
  let now = 50_000;
  const element = fakeElement();
  const pill = new BackgroundTaskPill({
    element,
    getThreadId: () => current,
    now: () => now,
    setInterval: () => 1,
    clearInterval: () => {},
  });

  pill.handleFrame({ type: 'background_task', status: 'running', task_id: 'r1', thread_id: 't1',
    label: 'Research', started_at: 40 });
  assert.equal(element.classList.contains('hidden'), false);
  assert.equal(element.textContent, 'Research running · 0:10');

  // Another thread's research does not show here...
  pill.handleFrame({ type: 'background_task', status: 'running', task_id: 'r2', thread_id: 't2',
    label: 'Research', started_at: 45 });
  assert.equal(element.textContent, 'Research running · 0:10');
  // ...but does when the user switches to that thread.
  current = 't2';
  pill.render();
  assert.equal(element.textContent, 'Research running · 0:05');

  current = 't1';
  pill.handleFrame({ type: 'background_task', status: 'finished', task_id: 'r1', thread_id: 't1' });
  assert.equal(element.classList.contains('hidden'), true);
});

test('pill reports whether the visible chat has research running, and says when that changes', () => {
  let current = 't1';
  const changes = [];
  const pill = new BackgroundTaskPill({
    element: fakeElement(),
    getThreadId: () => current,
    setInterval: () => 1,
    clearInterval: () => {},
    onChange: () => changes.push(pill.hasTasks()),
  });
  assert.equal(pill.hasTasks(), false);
  pill.handleFrame({ type: 'background_task', status: 'running', task_id: 'r1', thread_id: 't1', started_at: 1 });
  assert.equal(pill.hasTasks(), true);
  current = 't2';
  pill.render();
  assert.equal(pill.hasTasks(), false);
  assert.deepEqual(changes, [true, false]);
});

function handlerWith(chatApp) {
  globalThis.CustomEvent = class CustomEvent {
    constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
  };
  globalThis.window = { dispatchEvent: () => {}, chatApp };
  return new MessageHandler({ getThreadId: () => 'thread-current' }, {}, null, null, null, null, {});
}

test('background_task frames reach the pill even for a thread not on screen', () => {
  const seen = [];
  const handler = handlerWith({ handleBackgroundTask: (d) => seen.push(d.task_id) });
  handler.handleMessage({ type: 'background_task', status: 'running', task_id: 'a', thread_id: 'thread-other', seq: 1 });
  handler.handleMessage({ type: 'background_task', status: 'running', task_id: 'b', thread_id: 'thread-current', seq: 1 });
  assert.deepEqual(seen, ['a', 'b']);
});

test('run_started spins up the visible chat for a turn the user did not start', () => {
  const started = [];
  const handler = handlerWith({ handleRunStarted: () => started.push(true) });
  handler.handleMessage({ type: 'run_started', thread_id: 'thread-other', seq: 1 });
  assert.equal(started.length, 0);
  handler.handleMessage({ type: 'run_started', thread_id: 'thread-current', seq: 1 });
  assert.equal(started.length, 1);
});

test('a new turn starts a fresh bubble, not the previous reply plus the new text', () => {
  // Live bug: the results turn rendered "I've started the research…There are two
  // model files" — a new bubble seeded with the previous reply's text.
  const appState = {
    getThreadId: () => 'thread-current',
    currentAiMessage: { old: true },
    currentAiMessageBuffer: "I've started the research.",
    resetMessageState() { this.currentAiMessage = null; this.currentAiMessageBuffer = ''; },
  };
  globalThis.CustomEvent = class CustomEvent {
    constructor(type, options = {}) { this.type = type; this.detail = options.detail; }
  };
  globalThis.window = { dispatchEvent: () => {}, chatApp: { handleRunStarted: () => {} } };
  const handler = new MessageHandler(appState, {}, null, null, null, null, {});
  handler.handleMessage({ type: 'run_started', thread_id: 'thread-current', seq: 1 });
  assert.equal(appState.currentAiMessage, null);
  assert.equal(appState.currentAiMessageBuffer, '');
});

test('research results in history are recognised (not shown as a user bubble)', () => {
  assert.equal(isTaskNotification({ type: 'human', additional_kwargs: { task_notification: true } }), true);
  assert.equal(isTaskNotification({ type: 'human', content: 'hi' }), false);
  assert.equal(isTaskNotification({ type: 'ai', additional_kwargs: { task_notification: true } }), false);
});

test('a coding task reads "Coding task running · M:SS"', () => {
  const now = 1_000_000;
  assert.equal(pillText([{ label: 'Coding task', kind: 'coding', startedAt: (now - 75_000) / 1000 }], now),
    'Coding task running · 1:15');
});

test('parallel tasks share one pill with a count', () => {
  const now = 1_000_000;
  const coding = (ago) => ({ label: 'Coding task', kind: 'coding', startedAt: (now - ago) / 1000 });
  assert.equal(pillText([coding(75_000), coding(10_000)], now), '2 coding tasks running · 1:15');
  const research = { label: 'Research', kind: 'research', startedAt: (now - 5_000) / 1000 };
  assert.equal(pillText([research, { ...research }], now), '2 research tasks running · 0:05');
  assert.equal(pillText([coding(75_000), research], now), '2 background tasks running · 1:15');
});

test('the history marker names the kind of task that finished', () => {
  assert.equal(taskNotificationMarker({ content: '<task-notification>\nCoding task ab12cd34 finished. This is its report' }),
    'Coding task finished');
  assert.equal(taskNotificationMarker({ content: '<task-notification>\nResearch ab12cd34 finished. These are' }),
    'Research finished');
  assert.equal(taskNotificationMarker({ content: 'something else' }), 'Background task finished');
});
