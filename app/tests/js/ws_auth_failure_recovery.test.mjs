// A failed WebSocket sign-in must recover or say so — never "thinking" forever.
//
// crm-4, 2026-10-04: a ws token cached before the 0.7.12 key change was
// rejected. The client only logged `auth_error`, sent the user's message into
// the rejected socket anyway, and never reconnected. The chat spun for 12
// minutes until a manual reload.
//
// Pairs with app/tests/test_websocket_auth_failure_closes.py (the server half).

import assert from 'node:assert/strict';
import test from 'node:test';

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

const store = new Map();
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k),
};

// /api/ws-token: each test queues the responses it wants.
let tokenResponses = [];
let tokenFetches = 0;
globalThis.fetch = async (url) => {
  if (!String(url).includes('/api/ws-token')) throw new Error(`unexpected fetch ${url}`);
  tokenFetches += 1;
  const r = tokenResponses.shift() ?? { status: 200, token: `fresh-${tokenFetches}` };
  return {
    ok: r.status === 200,
    status: r.status,
    json: async () => ({ token: r.token, expires_in: 1800 }),
  };
};

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    this.closedWith = null;
    FakeSocket.all.push(this);
  }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close(code) { this.closedWith = code ?? 1000; this.readyState = FakeSocket.CLOSED; }
  // test helpers
  open() { this.onopen?.(); }
  receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  serverClose(code) { this.readyState = FakeSocket.CLOSED; this.onclose?.({ code, reason: '', wasClean: true }); }
}
FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSED = 3;
FakeSocket.all = [];
globalThis.WebSocket = FakeSocket;

const { WebSocketManager } = await import('../../frontend/chat/websocket/WebSocketManager.js');

const tick = () => new Promise((r) => setTimeout(r, 0));
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const events = (type) => dispatched.filter((e) => e.type === type);

function setup({ cachedToken = 'stale-token', ackTimeoutMs = 10_000 } = {}) {
  dispatched.length = 0;
  FakeSocket.all = [];
  tokenResponses = [];
  tokenFetches = 0;
  store.clear();
  if (cachedToken) {
    store.set('llamabot_ws_token', cachedToken);
    store.set('llamabot_ws_token_expiry', String(Date.now() + 25 * 60 * 1000));
  }
  const mgr = new WebSocketManager({ handleMessage() {} }, {
    websocketUrl: 'ws://box/ws', reconnectDelay: 3000, ackTimeoutMs,
  }, {});
  mgr.connect();
  return mgr;
}

const chatMessage = (id = 'cmid-1') => ({ message: 'hi', thread_id: 't1', client_message_id: id });
const userFrames = (sock) => sock.sent.filter((f) => f.client_message_id);

test('a message is not sent before the socket has signed in', async () => {
  const mgr = setup();
  const sock = FakeSocket.all[0];
  sock.open();
  await tick();
  assert.equal(sock.sent[0].type, 'auth');

  mgr.send(chatMessage());
  assert.equal(userFrames(sock).length, 0, 'held until auth_success');

  sock.receive({ type: 'auth_success', user: 'kody' });
  assert.equal(userFrames(sock).length, 1, 'delivered once signed in');
  mgr.disconnect();
});

test('auth_error fetches a fresh token and reconnects right away', async () => {
  const mgr = setup();
  const first = FakeSocket.all[0];
  first.open();
  await tick();
  mgr.send(chatMessage());

  first.receive({ type: 'auth_error', content: 'Invalid or expired token' });
  await tick(); await tick();

  assert.equal(events('websocketReauthenticating').length, 1, 'visible status');
  assert.equal(tokenFetches, 1, 'fresh token, not the cached one');
  assert.equal(FakeSocket.all.length, 2, 'reconnected without the 3s wait');
  assert.ok(first.closedWith, 'the rejected socket is closed');

  const second = FakeSocket.all[1];
  second.open();
  await tick();
  assert.deepEqual(second.sent[0], { type: 'auth', token: 'fresh-1' });
  assert.equal(userFrames(first).length, 0, 'never sent into the rejected socket');

  second.receive({ type: 'auth_success', user: 'kody' });
  assert.equal(userFrames(second).length, 1, 'the held message goes out on the good socket');
  assert.equal(events('websocketAuthFailed').length, 0);
  mgr.disconnect();
});

test('a server close with 4401 is treated the same as auth_error', async () => {
  const mgr = setup();
  const first = FakeSocket.all[0];
  first.open();
  await tick();

  first.serverClose(4401);
  await tick(); await tick();

  assert.equal(tokenFetches, 1);
  assert.equal(FakeSocket.all.length, 2);
  assert.equal(mgr.reconnectTimer, null, 'no extra 3s reconnect on top');
  mgr.disconnect();
});

test('auth_error followed by the 4401 close reconnects only once', async () => {
  const mgr = setup();
  const first = FakeSocket.all[0];
  first.open();
  await tick();

  first.receive({ type: 'auth_error', content: 'Invalid or expired token' });
  first.serverClose(4401);
  await tick(); await tick();

  assert.equal(tokenFetches, 1);
  assert.equal(FakeSocket.all.length, 2);
  mgr.disconnect();
});

test('a second auth_error in a row stops retrying and shows the error', async () => {
  const mgr = setup();
  FakeSocket.all[0].open();
  await tick();
  FakeSocket.all[0].receive({ type: 'auth_error', content: 'bad' });
  await tick(); await tick();

  const second = FakeSocket.all[1];
  second.open();
  await tick();
  second.receive({ type: 'auth_error', content: 'bad again' });
  second.serverClose(4401);
  await tick(); await tick();

  assert.equal(events('websocketAuthFailed').length, 1);
  assert.equal(FakeSocket.all.length, 2, 'no third socket');
  assert.equal(mgr.reconnectTimer, null, 'no reconnect loop');

  mgr.send(chatMessage('cmid-2'));
  assert.equal(FakeSocket.all.length, 2, 'sending does not restart the loop');
  mgr.disconnect();
});

test('a dead browser session (/api/ws-token 401) shows the error at once', async () => {
  const mgr = setup();
  FakeSocket.all[0].open();
  await tick();
  tokenResponses = [{ status: 401 }];

  FakeSocket.all[0].receive({ type: 'auth_error', content: 'bad' });
  await tick(); await tick();

  assert.equal(events('websocketAuthFailed').length, 1);
  assert.equal(FakeSocket.all.length, 1, 'no reconnect with no token to offer');
  mgr.disconnect();
});

test('a good sign-in resets the one-retry budget', async () => {
  const mgr = setup();
  FakeSocket.all[0].open();
  await tick();
  FakeSocket.all[0].receive({ type: 'auth_error', content: 'bad' });
  await tick(); await tick();
  FakeSocket.all[1].open();
  await tick();
  FakeSocket.all[1].receive({ type: 'auth_success', user: 'kody' });

  // Much later the token is rejected again (e.g. another key rotation).
  FakeSocket.all[1].receive({ type: 'auth_error', content: 'bad' });
  await tick(); await tick();

  assert.equal(events('websocketAuthFailed').length, 0);
  assert.equal(FakeSocket.all.length, 3);
  mgr.disconnect();
});

test('a message with no ack in time is reported as not delivered', async () => {
  const mgr = setup({ ackTimeoutMs: 20 });
  const sock = FakeSocket.all[0];
  sock.open();
  await tick();
  sock.receive({ type: 'auth_success', user: 'kody' });

  const msg = chatMessage('cmid-lost');
  mgr.send(msg);
  await sleep(40);

  const undelivered = events('websocketMessageUndelivered');
  assert.equal(undelivered.length, 1);
  assert.equal(undelivered[0].detail.data, msg, 'carries the exact payload for Retry');
  mgr.disconnect();
});

test('an acked message is never reported', async () => {
  const mgr = setup({ ackTimeoutMs: 20 });
  const sock = FakeSocket.all[0];
  sock.open();
  await tick();
  sock.receive({ type: 'auth_success', user: 'kody' });

  mgr.send(chatMessage('cmid-ok'));
  sock.receive({ type: 'ack', client_message_id: 'cmid-ok', status: 'accepted' });
  await sleep(40);

  assert.equal(events('websocketMessageUndelivered').length, 0);
  mgr.disconnect();
});

test('the ack clock starts at send time, even while held for sign-in', async () => {
  // The crm-4 run never started, so nothing server-side could stop the spinner.
  const mgr = setup({ ackTimeoutMs: 20 });
  FakeSocket.all[0].open();
  await tick();
  mgr.send(chatMessage('cmid-held'));
  await sleep(40);

  assert.equal(events('websocketMessageUndelivered').length, 1);
  mgr.disconnect();
});

test('Retry re-sends the same client_message_id', async () => {
  const mgr = setup({ ackTimeoutMs: 20 });
  const sock = FakeSocket.all[0];
  sock.open();
  await tick();
  sock.receive({ type: 'auth_success', user: 'kody' });

  const msg = chatMessage('cmid-retry');
  mgr.send(msg);
  await sleep(40);
  mgr.retryMessage(events('websocketMessageUndelivered')[0].detail.data);

  const ids = userFrames(sock).map((f) => f.client_message_id);
  assert.deepEqual(ids, ['cmid-retry', 'cmid-retry'], 'server de-dupes on the same id');
  mgr.disconnect();
});

test('an error reply instead of an ack does not also show "not delivered"', async () => {
  const mgr = setup({ ackTimeoutMs: 20 });
  const sock = FakeSocket.all[0];
  sock.open();
  await tick();
  sock.receive({ type: 'auth_success', user: 'kody' });

  mgr.send(chatMessage('cmid-denied'));
  sock.receive({ type: 'error', content: "You don't have access to that mode." });
  await sleep(40);

  assert.equal(events('websocketMessageUndelivered').length, 0);
  mgr.disconnect();
});

test('the Rails relay (ActionCable) gets no ack timer', () => {
  const mgr = new WebSocketManager({}, { ackTimeoutMs: 20 }, {});
  mgr.isActionCable = true;
  mgr.armAckTimer(chatMessage('cmid-cable'));
  assert.equal(mgr.pendingAcks.size, 0);
});

test('after sign-in has given up, a new message fails at once, not after the timeout', async () => {
  const mgr = setup({ ackTimeoutMs: 10_000 });
  FakeSocket.all[0].open();
  await tick();
  tokenResponses = [{ status: 401 }];
  FakeSocket.all[0].receive({ type: 'auth_error', content: 'bad' });
  await tick(); await tick();

  const msg = chatMessage('cmid-after-giveup');
  mgr.send(msg);
  assert.equal(events('websocketMessageUndelivered').length, 0, 'not synchronously: the caller sets running after send()');
  await tick();

  const undelivered = events('websocketMessageUndelivered');
  assert.equal(undelivered.length, 1, 'no 10s spinner first');
  assert.equal(undelivered[0].detail.data, msg);
  assert.equal(mgr.pendingAcks.size, 0);
  mgr.disconnect();
});

test('giving up on sign-in leaves Send usable, even when the close beat the token fetch', async () => {
  // Real-browser race: the server's 4401 close lands while /api/ws-token is
  // still in flight. handleClose disables Send; nothing re-enabled it.
  dispatched.length = 0;
  FakeSocket.all = [];
  store.clear();
  let answer;
  const realFetch = globalThis.fetch;
  globalThis.fetch = () => new Promise((r) => { answer = r; });
  try {
    const sendButton = { disabled: false };
    const mgr = new WebSocketManager({ handleMessage() {} }, { websocketUrl: 'ws://box/ws' }, { sendButton });
    store.set('llamabot_ws_token', 'stale-token');
    store.set('llamabot_ws_token_expiry', String(Date.now() + 25 * 60 * 1000));
    mgr.connect();
    const sock = FakeSocket.all[0];
    sock.open();
    await tick();

    sock.receive({ type: 'auth_error', content: 'bad' });
    sock.serverClose(4401);
    assert.equal(sendButton.disabled, true, 'disabled while recovering');

    answer({ ok: false, status: 401, json: async () => ({}) });
    await tick(); await tick();

    assert.equal(events('websocketAuthFailed').length, 1);
    assert.equal(sendButton.disabled, false, 'Send works again so Retry is reachable');
    mgr.disconnect();
  } finally {
    globalThis.fetch = realFetch;
  }
});

// --- index.js wiring: the events above must reach the screen ---------------
import { readFileSync } from 'node:fs';
const indexSrc = readFileSync(new URL('../../frontend/chat/index.js', import.meta.url), 'utf8');
const listener = (name) => {
  const at = indexSrc.indexOf(`addEventListener('${name}'`);
  assert.ok(at > 0, `index.js listens for ${name}`);
  return indexSrc.slice(at, at + 1500);
};

test('UI: reconnecting shows a visible status line', () => {
  assert.match(listener('websocketReauthenticating'), /Reconnecting to Leo/);
});

test('UI: sign-in failure stops the spinner and offers Refresh', () => {
  const block = listener('websocketAuthFailed');
  assert.match(block, /setAgentRunning\(false\)/);
  assert.match(block, /Leo could not sign you in/);
  assert.match(block, /location\.reload\(\)/);
});

test('UI: an undelivered message stops the spinner and offers Retry', () => {
  const block = listener('websocketMessageUndelivered');
  assert.match(block, /setAgentRunning\(false\)/);
  assert.match(block, /Your message was not delivered/);
  assert.match(block, /retryMessage\(/);
});
