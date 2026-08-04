// The diagnostics buffer rides along with user feedback to the mothership, so its
// bounding and redaction are the properties that matter most.

import assert from 'node:assert/strict';
import test from 'node:test';

import { LeoDiagnostics, redact } from '../../frontend/chat/utils/LeoDiagnostics.js';

function stubGlobals() {
  globalThis.location = { href: 'https://leo-test.llamapress.ai/chat?thread=abc' };
  globalThis.navigator = { userAgent: 'Mozilla/5.0 (test)' };
}

test('caps the buffer at 100 events', () => {
  stubGlobals();
  const d = new LeoDiagnostics();
  for (let i = 0; i < 250; i++) d.record('tick', { i });

  assert.equal(d.snapshot().recent_events.length, 100);
});

test('drops events older than the 5-minute window', () => {
  stubGlobals();
  let now = 1_000_000;
  const d = new LeoDiagnostics({ now: () => now });

  d.record('old_event');
  now += 6 * 60 * 1000;
  d.record('fresh_event');

  const events = d.snapshot().recent_events.map((e) => e.event);
  assert.deepEqual(events, ['fresh_event']);
});

test('redacts token-like strings before storing them', () => {
  assert.match(redact('Authorization: Bearer abc123def456ghi'), /REDACTED/);
  assert.match(redact('api_key=sk-abcdefghijklmnop'), /REDACTED/);
  assert.match(redact('here is a jwt eyJhbGciOi.eyJzdWIiOi.SflKxwRJSM'), /REDACTED/);
  assert.match(redact('session 0123456789abcdef0123456789abcdef'), /REDACTED/);

  assert.doesNotMatch(redact('WebSocket closed with code 1006'), /REDACTED/);
});

test('console capture redacts and does not swallow the original call', () => {
  stubGlobals();
  const d = new LeoDiagnostics();
  const seen = [];
  const fakeConsole = { warn: (...a) => seen.push(a.join(' ')), error: () => {} };

  d.patchConsole(fakeConsole);
  fakeConsole.warn('auth failed for token=sk-supersecretvalue123');

  assert.equal(seen.length, 1, 'the original console.warn must still run');
  const stored = d.snapshot().recent_events.find((e) => e.event === 'console');
  assert.ok(stored);
  assert.doesNotMatch(stored.message, /supersecretvalue/);
});

test('captures the websocket close details support actually needs', () => {
  stubGlobals();
  const d = new LeoDiagnostics();

  d.noteOpen(1);
  d.noteClose({ code: 1006, reason: '', wasClean: false, readyState: 3 });
  d.noteReconnect(2, 5);
  d.noteOutbox(1, 'queue');

  const snap = d.snapshot({ threadId: 't-1', agentMode: 'rails_ticket_mode_agent', llmModel: 'deepseek-v4-flash' });

  assert.equal(snap.connection.last_close.code, 1006);
  assert.equal(snap.connection.last_close.wasClean, false);
  assert.equal(snap.connection.reconnect_attempts, 2);
  assert.equal(snap.connection.outbox_length, 1);
  assert.ok(snap.connection.last_connected_at);
  assert.ok(snap.connection.last_disconnected_at);
  assert.equal(snap.thread_id, 't-1');
  assert.equal(snap.agent_mode, 'rails_ticket_mode_agent');
  assert.equal(snap.llm_model, 'deepseek-v4-flash');
});

test('truncates long strings so a snapshot stays small', () => {
  stubGlobals();
  const d = new LeoDiagnostics();
  d.record('big', { message: 'x'.repeat(10000) });

  const stored = d.snapshot().recent_events[0];
  assert.ok(stored.message.length < 400, `stored ${stored.message.length} chars`);
});

test('a whole snapshot stays comfortably bounded', () => {
  stubGlobals();
  const d = new LeoDiagnostics();
  for (let i = 0; i < 300; i++) {
    d.record('noise', { message: 'y'.repeat(1000), i });
  }

  const bytes = JSON.stringify(d.snapshot()).length;
  assert.ok(bytes < 64_000, `snapshot was ${bytes} bytes`);
});

test('recording never throws on hostile input', () => {
  stubGlobals();
  const d = new LeoDiagnostics();
  const circular = {};
  circular.self = circular;

  assert.doesNotThrow(() => d.record('weird', { circular }));
  assert.doesNotThrow(() => d.record('weird', { fn: () => {} }));
});
