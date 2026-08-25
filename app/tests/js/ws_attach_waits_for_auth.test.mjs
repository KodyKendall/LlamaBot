// `attach` must not race the auth handshake.
//
// The server now refuses control frames until the socket has authenticated.
// The client used to fire `attach` on a 300ms timer after `websocketConnected`,
// hoping the async token fetch had finished first — when it hadn't, the resumed
// run lost its live output and the spinner hung. `websocketReady` is the real
// signal: it fires on `auth_success`, or immediately for ActionCable (which the
// Rails gem has already authenticated).
import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const appRoot = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const manager = readFileSync(join(appRoot, 'frontend/chat/websocket/WebSocketManager.js'), 'utf8');
const index = readFileSync(join(appRoot, 'frontend/chat/index.js'), 'utf8');

test('auth_success announces readiness', () => {
  const block = manager.slice(manager.indexOf("data.type === 'auth_success'"));
  assert.match(block.slice(0, 400), /this\.announceReady\(\)/);
});

test('ActionCable is ready as soon as it connects', () => {
  const block = manager.slice(manager.indexOf('ActionCable is authenticated by the Rails gem'));
  assert.match(block.slice(0, 200), /this\.announceReady\(\)/);
});

test('a box with no token still becomes ready', () => {
  // WS_AUTH_REQUIRED=false: no token is ever coming, so waiting would strand
  // the resume path forever.
  const block = manager.slice(manager.indexOf('No auth token available'));
  assert.match(block.slice(0, 300), /this\.announceReady\(\)/);
});

test('announceReady dispatches websocketReady', () => {
  assert.match(manager, /announceReady\(\)\s*\{\s*window\.dispatchEvent\(new CustomEvent\('websocketReady'\)\)/);
});

test('attach listens for websocketReady, not websocketConnected', () => {
  const block = index.slice(index.indexOf("type: 'attach'") - 900, index.indexOf("type: 'attach'") + 200);
  assert.match(block, /addEventListener\('websocketReady'/);
  assert.doesNotMatch(block, /addEventListener\('websocketConnected'/);
});

test('the 300ms guess is gone', () => {
  const block = index.slice(index.indexOf("type: 'attach'") - 900, index.indexOf("type: 'attach'") + 200);
  assert.doesNotMatch(block, /setTimeout/);
});
