// LP-23: a signed-out user asking a custom Leo mode for app data got
// "this is a setup issue" back, because the token fetch's 401 was swallowed
// into "no token" — indistinguishable from a broken agent. The fetch now
// reports WHY there's no token, and that rides the frame as `rails_auth`.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

import { fetchRailsApiToken } from '../../frontend/chat/utils/railsApiToken.js';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');

const respond = (status, body = {}) => async () => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

test('a token means signed in', async () => {
  const r = await fetchRailsApiToken('http://rails', respond(200, { api_token: 't', expires_in: 60 }));
  assert.equal(r.token, 't');
  assert.equal(r.expiresIn, 60);
  assert.equal(r.railsAuth, 'signed_in');
});

test('401 means signed out', async () => {
  const r = await fetchRailsApiToken('http://rails', respond(401));
  assert.equal(r.token, null);
  assert.equal(r.railsAuth, 'signed_out');
});

test('5xx is an error, not signed out', async () => {
  const r = await fetchRailsApiToken('http://rails', respond(502));
  assert.equal(r.token, null);
  assert.equal(r.railsAuth, 'error');
});

test('a network/CORS failure is an error and never throws', async () => {
  const r = await fetchRailsApiToken('http://rails', async () => { throw new TypeError('Failed to fetch'); });
  assert.equal(r.token, null);
  assert.equal(r.railsAuth, 'error');
});

test('the send path puts rails_auth on the frame', () => {
  assert.match(INDEX_JS, /messageData\.rails_auth\s*=/);
});
