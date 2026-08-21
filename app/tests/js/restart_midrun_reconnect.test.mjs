// Reproduction (browser half): an in-place update restarts the llamabot
// container while a run is streaming. What the tab does about it.
//
// Pairs with app/tests/test_restart_midrun.py (the server half).
//
// The two properties that decide whether the user's session survives:
//   1. how long the socket keeps retrying (the restart budget), and
//   2. what happens when it gets back and the run is gone.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (...p) => readFileSync(resolve(APP_ROOT, ...p), 'utf8');

// --- globals the chat modules expect at import time ------------------------
const dispatched = [];
globalThis.location = { href: 'https://box.llamapress.ai/chat', host: 'box.llamapress.ai', protocol: 'https:' };
globalThis.navigator = { userAgent: 'node-test' };
globalThis.CustomEvent = class CustomEvent {
  constructor(type, init = {}) { this.type = type; this.detail = init.detail; }
};
globalThis.window = {
  location: globalThis.location,
  addEventListener() {},
  dispatchEvent(e) { dispatched.push(e); return true; },
};
globalThis.document = { addEventListener() {}, querySelector: () => null, cookie: '' };

const { WebSocketManager } = await import('../../frontend/chat/websocket/WebSocketManager.js');
const { DEFAULT_CONFIG } = await import('../../frontend/chat/config.js');

/** Socket that never opens — the container is down for the whole restart. */
class DeadSocket {
  constructor(url) { this.url = url; DeadSocket.created += 1; }
  close() {}
}
DeadSocket.created = 0;
globalThis.WebSocket = DeadSocket;
globalThis.WebSocket.CONNECTING = 0;
globalThis.WebSocket.OPEN = 1;
globalThis.WebSocket.CLOSED = 3;

test('the restart budget is 30 attempts x 3s = 90 seconds', () => {
  assert.equal(DEFAULT_CONFIG.maxReconnectAttempts, 30);
  assert.equal(DEFAULT_CONFIG.reconnectDelay, 3000);
});

test('a container restart is retried for ~90s, then gives up with one event', () => {
  const delays = [];
  let queue = [];
  const realSetTimeout = globalThis.setTimeout;
  const realClearTimeout = globalThis.clearTimeout;
  globalThis.setTimeout = (fn, ms) => { delays.push(ms); queue.push(fn); return queue.length; };
  globalThis.clearTimeout = () => {};

  try {
    dispatched.length = 0;
    DeadSocket.created = 0;

    const mgr = new WebSocketManager({}, { ...DEFAULT_CONFIG, websocketUrl: 'ws://box/ws' }, {});
    mgr.connect();

    // Each attempt: the socket closes (1006, container gone), a reconnect is
    // scheduled, the timer fires and dials again.
    for (let i = 0; i < 200; i++) {
      if (dispatched.some((e) => e.type === 'websocketReconnectFailed')) break;
      mgr.handleClose({ code: 1006, reason: '', wasClean: false });
      queue.splice(0).forEach((fn) => fn());
    }

    const failures = dispatched.filter((e) => e.type === 'websocketReconnectFailed');
    assert.equal(failures.length, 1, 'gives up exactly once, not on every retry');
    assert.equal(failures[0].detail.attempts, 30);
    assert.equal(delays.length, 30, 'one timer per attempt');
    assert.ok(delays.every((d) => d === 3000), 'fixed delay — no backoff, no jitter');
    assert.equal(delays.reduce((a, b) => a + b, 0), 90000);
    // 30 dials after the initial connect.
    assert.equal(DeadSocket.created, 31);
  } finally {
    globalThis.setTimeout = realSetTimeout;
    globalThis.clearTimeout = realClearTimeout;
  }
});

test('the spinner is deliberately left running while retrying', () => {
  // handleClose emits `websocketDisconnected` but nothing that stops the
  // thinking indicator — that only happens on the *failed* event (index.js).
  const src = read('frontend', 'chat', 'websocket', 'WebSocketManager.js');
  const handleClose = src.slice(src.indexOf('handleClose(event) {'), src.indexOf('handleError(error) {'));
  assert.match(handleClose, /websocketDisconnected/);
  assert.doesNotMatch(handleClose, /hideThinkingIndicator/);
});

test('the update never sends a stale attach: attach is non-queueable', () => {
  const src = read('frontend', 'chat', 'websocket', 'WebSocketManager.js');
  assert.match(src, /NON_QUEUEABLE_TYPES = new Set\(\['auth', 'cancel', 'attach'\]\)/);
});

// --- what the tab does once the new container answers ----------------------

test('no_active_run raises websocketReplayUnavailable', async () => {
  const { MessageHandler } = await import('../../frontend/chat/websocket/MessageHandler.js');
  const handler = Object.create(MessageHandler.prototype);
  handler.appState = { getThreadId: () => 'thread-1' };
  handler._lastSeqByThread = {};

  dispatched.length = 0;
  handler.handleMessage({ type: 'no_active_run', thread_id: 'thread-1' });

  assert.deepEqual(dispatched.map((e) => e.type), ['websocketReplayUnavailable']);
});

test('that event is what stops the spinner and shows "Lost connection"', () => {
  const index = read('frontend', 'chat', 'index.js');
  const listener = index.slice(
    index.indexOf("addEventListener('websocketReplayUnavailable'"),
    index.indexOf("addEventListener('websocketActivity'"),
  );
  assert.match(listener, /hideThinkingIndicator\(\)/);
  assert.match(listener, /setAgentRunning\(false\)/);

  // hideThinkingIndicator is the single place the user-visible error + the
  // FrontendConnectionLost report come from, and only when a run was live.
  const hide = index.slice(index.indexOf('hideThinkingIndicator() {'), index.indexOf('hideThinkingIndicator() {') + 2500);
  assert.match(hide, /wasThinking/);
  assert.match(hide, /FrontendConnectionLost/);
  assert.match(hide, /renderErrorMessage\('Lost connection'\)/);
});

// --- the telemetry gap -----------------------------------------------------

test('the disconnect report is fire-and-forget: no retry, no queue', async () => {
  const { ErrorReporter } = await import('../../frontend/chat/utils/ErrorReporter.js');
  const calls = [];
  globalThis.fetch = (url, opts) => {
    calls.push({ url, body: JSON.parse(opts.body) });
    return Promise.reject(new Error('Failed to fetch'));   // box still down
  };

  const reporter = new ErrorReporter({ getThreadId: () => 't1' });
  reporter.report('FrontendConnectionLost', 'Lost connection mid-run (thinking indicator active)');
  await new Promise((r) => setTimeout(r, 10));

  assert.equal(calls.length, 1, 'one POST...');
  assert.equal(calls[0].url, '/api/frontend-error');

  // ...and the failure is swallowed: nothing is retained to send later.
  const src = read('frontend', 'chat', 'utils', 'ErrorReporter.js');
  assert.match(src, /\.catch\(\(\) => \{\}\)/);
  assert.doesNotMatch(src, /localStorage|sessionStorage|retry/i);

  // Worse: the fingerprint is already burned, so a later retry is deduped away.
  reporter.report('FrontendConnectionLost', 'Lost connection mid-run (thinking indicator active)');
  assert.equal(calls.length, 1, 'the second attempt is dropped as a duplicate');
});

test('nothing guards the update button against an in-flight run', () => {
  const html = read('frontend', 'chat.html');
  const start = html.indexOf('async function startUpdate()');
  const body = html.slice(start, html.indexOf('// ---- wiring ----', start));
  assert.ok(start !== -1);
  assert.doesNotMatch(body, /isAgentRunning|thinkingArea|agentRunning/);
});
