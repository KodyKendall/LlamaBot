// LP-30: a reopened chat tab must get a NEW sign-in token on its retry.
//
// duke-homes, 2026-11-03: the box rejected the same expired token twice, with no
// /api/ws-token request between the two tries. A tab restored after a discard loads like
// a history navigation, where Chrome serves a stored response without checking it, so the
// retry's fetch got the old token back from the browser cache.
//
// The fake fetch below plays that browser cache: unless a request says cache: 'no-store',
// it returns the response it stored earlier instead of asking the server.

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

// --- JWTs -------------------------------------------------------------------
const b64url = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64url');
const nowSec = () => Math.floor(Date.now() / 1000);
/** A JWT-shaped token. The signature is never checked client-side. */
let jti = 0;
function jwt({ expIn = 30 * 60, iatAgo = 0, extra = {} } = {}) {
  jti += 1; // tokens issued in the same second still differ, as two server tokens would
  const payload = { sub: 'kody', type: 'ws_auth', jti, iat: nowSec() - iatAgo, exp: nowSec() + expIn, ...extra };
  return `${b64url({ alg: 'HS256', typ: 'JWT' })}.${b64url(payload)}.c2lnbmF0dXJl`;
}

// --- server + browser cache ---------------------------------------------------
let serverTokens = [];       // what the box would issue next
let serverRequests = 0;      // requests that actually reached the box
let browserCache = null;     // the stored /api/ws-token response, if any
let fetchCalls = [];
globalThis.fetch = async (url, opts = {}) => {
  if (!String(url).includes('/api/ws-token')) throw new Error(`unexpected fetch ${url}`);
  fetchCalls.push({ url: String(url), cache: opts.cache });
  if (browserCache && opts.cache !== 'no-store') return browserCache();
  serverRequests += 1;
  const next = serverTokens.shift() ?? { status: 200, token: jwt() };
  const response = () => ({
    ok: next.status === 200,
    status: next.status,
    json: async () => ({ token: next.token, expires_in: next.expiresIn ?? 1800 }),
  });
  if (next.status === 200 && opts.cache !== 'no-store') browserCache = response;
  return response();
};

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.readyState = FakeSocket.OPEN;
    this.sent = [];
    FakeSocket.all.push(this);
  }
  send(raw) { this.sent.push(JSON.parse(raw)); }
  close(code) { this.closedWith = code ?? 1000; this.readyState = FakeSocket.CLOSED; }
  open() { this.onopen?.(); }
  receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  serverClose(code) { this.readyState = FakeSocket.CLOSED; this.onclose?.({ code, reason: '', wasClean: true }); }
}
FakeSocket.CONNECTING = 0;
FakeSocket.OPEN = 1;
FakeSocket.CLOSED = 3;
FakeSocket.all = [];
globalThis.WebSocket = FakeSocket;

const { TokenManager } = await import('../../frontend/chat/auth/TokenManager.js');
const { WebSocketManager, authFailureReport } = await import('../../frontend/chat/websocket/WebSocketManager.js');
const { ErrorReporter } = await import('../../frontend/chat/utils/ErrorReporter.js');

const tick = () => new Promise((r) => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 6; i++) await tick(); };
const events = (type) => dispatched.filter((e) => e.type === type);
const authToken = (sock) => sock.sent.find((f) => f.type === 'auth')?.token;

function reset() {
  dispatched.length = 0;
  FakeSocket.all = [];
  serverTokens = [];
  serverRequests = 0;
  browserCache = null;
  fetchCalls = [];
  store.clear();
}

function cache(token, expiryMs = Date.now() + 25 * 60 * 1000) {
  store.set('llamabot_ws_token', token);
  store.set('llamabot_ws_token_expiry', String(expiryMs));
}

function connect() {
  const mgr = new WebSocketManager({ handleMessage() {} }, {
    websocketUrl: 'ws://box/ws', reconnectDelay: 3000, ackTimeoutMs: 10_000,
  }, {});
  mgr.connect();
  return mgr;
}

// --- FR-2: the retry asks the server ----------------------------------------

test('fetchNewToken asks the server, never the browser cache', async () => {
  reset();
  await TokenManager.fetchNewToken();
  assert.deepEqual(fetchCalls, [{ url: '/api/ws-token', cache: 'no-store' }]);
});

test('T-2: after auth_error the retry signs in with a new token, not the cached old one', async () => {
  reset();
  // The tab's last token request, hours ago, left this in the browser cache.
  const old = jwt({ expIn: -60 * 60, iatAgo: 90 * 60 });
  browserCache = () => ({ ok: true, status: 200, json: async () => ({ token: old, expires_in: 1800 }) });
  const issuedFirst = jwt();
  const issuedOnRetry = jwt();
  serverTokens = [{ status: 200, token: issuedFirst }, { status: 200, token: issuedOnRetry }];

  const mgr = connect();
  FakeSocket.all[0].open();
  await settle();
  const first = authToken(FakeSocket.all[0]);
  assert.notEqual(first, old, 'the first try is not served the cached old token either');
  FakeSocket.all[0].receive({ type: 'auth_error', content: 'Invalid or expired token' });
  await settle();

  const second = FakeSocket.all[1];
  assert.ok(second, 'reconnected');
  second.open();
  await settle();
  assert.equal(authToken(second), issuedOnRetry, 'the retry uses the token the server just issued');
  assert.notEqual(authToken(second), first);
  assert.equal(serverRequests, 2, 'both tries reached the server');
  mgr.disconnect();
});

// --- FR-3, FR-5: the stored token is judged by its own exp ------------------

const MIN = 60;
for (const [label, expIn, storedFuture, usable] of [
  ['30 minutes away', 30 * MIN, true, true],
  ['6 minutes away', 6 * MIN, true, true],
  ['exactly 5 minutes away', 5 * MIN, true, false],
  ['4 minutes away', 4 * MIN, true, false],
  ['10 minutes past', -10 * MIN, true, false],
  ['30 minutes away, stored expiry passed', 30 * MIN, false, false],
]) {
  test(`getCachedToken: exp ${label} -> ${usable ? 'the token' : 'no token'}`, () => {
    reset();
    const token = jwt({ expIn });
    cache(token, storedFuture ? Date.now() + 60 * 60 * 1000 : Date.now() - 1000);
    assert.equal(TokenManager.getCachedToken(), usable ? token : null);
  });
}

test('getCachedToken reads a payload that uses base64url - and _ characters', () => {
  reset();
  // These values encode to payload text containing both "-" and "_".
  const token = jwt({ extra: { pad: '>>>???~~~', more: 'ÿÿÿ~~>>' } });
  assert.match(token.split('.')[1], /[-_]/, 'the fixture really has base64url characters');
  cache(token);
  assert.equal(TokenManager.getCachedToken(), token);
});

for (const [label, value] of [
  ['not a JWT', 'stale-token'],
  ['a payload that is not JSON', `aGVhZGVy.${Buffer.from('nope').toString('base64url')}.sig`],
  ['no exp claim', `aGVhZGVy.${b64url({ sub: 'kody' })}.sig`],
  ['a non-numeric exp', `aGVhZGVy.${b64url({ exp: 'soon' })}.sig`],
]) {
  test(`getCachedToken treats ${label} as expired`, () => {
    reset();
    cache(value);
    assert.equal(TokenManager.getCachedToken(), null);
    assert.equal(store.has('llamabot_ws_token'), false, 'and clears it');
  });
}

// --- FR-6: a token just fetched is used as is ---------------------------------

test('with a 1-minute token lifetime the retry still uses the token it just fetched', async () => {
  reset();
  cache(jwt({ expIn: 20 * MIN }));
  const shortLived = jwt({ expIn: 60 });
  serverTokens = [{ status: 200, token: shortLived, expiresIn: 60 }];

  const mgr = connect();
  FakeSocket.all[0].open();
  await settle();
  FakeSocket.all[0].receive({ type: 'auth_error', content: 'Invalid or expired token' });
  await settle();
  FakeSocket.all[1].open();
  await settle();

  assert.equal(authToken(FakeSocket.all[1]), shortLived);
  assert.equal(serverRequests, 1, 'no second token request for the same retry');
  mgr.disconnect();
});

// --- FR-4, FR-7, FR-9: the failure report -------------------------------------

async function failTwice({ retryResponse }) {
  reset();
  cache(jwt({ expIn: 20 * MIN, iatAgo: 10 * MIN }));
  serverTokens = [retryResponse];
  const mgr = connect();
  FakeSocket.all[0].open();
  await settle();
  FakeSocket.all[0].receive({ type: 'auth_error', content: 'Invalid or expired token' });
  await settle();
  if (FakeSocket.all[1]) {
    FakeSocket.all[1].open();
    await settle();
    FakeSocket.all[1].receive({ type: 'auth_error', content: 'Invalid or expired token' });
    await settle();
  }
  mgr.disconnect();
  const failed = events('websocketAuthFailed');
  assert.equal(failed.length, 1);
  return failed[0].detail;
}

test('a failed retry reports that its token came from the network, and its age', async () => {
  const token = jwt({ iatAgo: 3 * MIN });
  const detail = await failTwice({ retryResponse: { status: 200, token } });
  const report = authFailureReport(detail);
  assert.match(report, /token from network/);
  assert.match(report, /age 3 min/);
  assert.ok(!report.includes(token), 'never the token itself');
  assert.ok(!report.includes(token.split('.')[1]), 'not even its payload');
});

test('a refused token request is reported with its HTTP status', async () => {
  const detail = await failTwice({ retryResponse: { status: 401 } });
  assert.match(authFailureReport(detail), /HTTP 401/);
});

test('a token with no iat reports minutes past exp instead', () => {
  const report = authFailureReport({
    reason: 'Invalid or expired token',
    token: { source: 'localStorage', iat: null, exp: nowSec() - 7 * MIN },
  });
  assert.match(report, /token from localStorage/);
  assert.match(report, /7 min past exp/);
});

// --- FR-8: the extra detail must not split the fingerprint --------------------

test('reports with different token ages keep one fingerprint', () => {
  const posts = [];
  const savedFetch = globalThis.fetch;
  globalThis.fetch = (url, opts) => { posts.push(JSON.parse(opts.body)); return Promise.resolve({ ok: true }); };
  try {
    const young = authFailureReport({ reason: 'Invalid or expired token', token: { source: 'network', iat: nowSec() - 60, exp: nowSec() + 1700 } });
    const old = authFailureReport({ reason: 'Invalid or expired token', token: { source: 'localStorage', iat: nowSec() - 900 * 60, exp: nowSec() - 870 * 60 } });
    assert.notEqual(young, old);
    // Two reporters: one per page load, as on two tabs.
    new ErrorReporter({ getThreadId: () => 't', getAgentConfig: () => ({ name: 'rails_agent' }) }).report('FrontendWsAuthFailed', young);
    new ErrorReporter({ getThreadId: () => 't', getAgentConfig: () => ({ name: 'rails_agent' }) }).report('FrontendWsAuthFailed', old);
  } finally {
    globalThis.fetch = savedFetch;
  }
  assert.equal(posts.length, 2);
  assert.equal(posts[0].fingerprint, posts[1].fingerprint);
});

test('index.js reports the sign-in failure through authFailureReport', async () => {
  const { readFileSync } = await import('node:fs');
  const src = readFileSync(new URL('../../frontend/chat/index.js', import.meta.url), 'utf8');
  const at = src.indexOf("addEventListener('websocketAuthFailed'");
  assert.ok(at > 0);
  assert.match(src.slice(at, at + 800), /authFailureReport\(/);
});
