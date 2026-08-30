// A dropped socket must not leave the customer reading a truncated answer (0.7.5).
//
// rsb-dev, 2026-08-28, thread 1787943690689-qzr1efuar: the socket died mid-run, the server
// kept streaming into a socket nobody read, and the chunks that arrived AFTER the reconnect
// were appended to a fresh bubble. The customer saw an answer starting mid-sentence —
// exactly the last 435 chars of a complete 906-char reply, the first 471 lost. The work was
// correct and fully persisted server-side; only their VIEW broke, and they had no way to
// know. 45 of the 57 boxes that can report this have hit it (78.9%).
//
// The rule under test: on reconnect while a run was in flight, re-sync the in-flight bubble
// against the server instead of appending live chunks to a gap.

import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { readFileSync } from 'node:fs';

import {
  shouldResumeAfterReconnect,
  pickAuthoritativeMessage,
  nextResumeGeneration,
  isStaleResume,
} from '../../frontend/chat/websocket/StreamResume.js';

const APP_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const WS_MANAGER = readFileSync(
  resolve(APP_ROOT, 'frontend', 'chat', 'websocket', 'WebSocketManager.js'), 'utf8');
const RENDERER = readFileSync(
  resolve(APP_ROOT, 'frontend', 'chat', 'messages', 'MessageRenderer.js'), 'utf8');
const INDEX_JS = readFileSync(resolve(APP_ROOT, 'frontend', 'chat', 'index.js'), 'utf8');

// The real numbers from the incident.
const FULL = 'A'.repeat(471) + ' updates from the server.\n- Pagination stays in-frame.';
const PARTIAL = FULL.slice(471);

test('resumes when the socket dropped while a run was in flight', () => {
  assert.equal(shouldResumeAfterReconnect({ wasRunInFlight: true, threadId: 't1' }), true);
});

test('does not resume when no run was in flight', () => {
  // An idle reconnect must not refetch and repaint the thread under the user.
  assert.equal(shouldResumeAfterReconnect({ wasRunInFlight: false, threadId: 't1' }), false);
});

test('does not resume without a thread to resume into', () => {
  assert.equal(shouldResumeAfterReconnect({ wasRunInFlight: true, threadId: null }), false);
});

test('picks the server message that the partial text is the tail of', () => {
  // This is the incident's own signature: full.end_with?(frag) was true at offset 471.
  const messages = [
    { type: 'human', content: 'fix the column sort' },
    { type: 'ai', content: 'an earlier unrelated answer' },
    { type: 'ai', content: FULL },
  ];

  const picked = pickAuthoritativeMessage(messages, { partialText: PARTIAL });

  assert.equal(picked, FULL);
  assert.ok(picked.length > PARTIAL.length, 'must recover the lost head, not keep the tail');
});

test('falls back to the last assistant message when nothing matches the fragment', () => {
  // The fragment can be unmatchable — a chunk boundary mid-token, or markdown the renderer
  // rewrote. The last assistant message is still strictly better than a mid-sentence bubble.
  const messages = [
    { type: 'ai', content: 'older' },
    { type: 'ai', content: 'the newest complete answer' },
  ];

  assert.equal(
    pickAuthoritativeMessage(messages, { partialText: 'nothing like this' }),
    'the newest complete answer',
  );
});

test('ignores tool and human messages when choosing the answer', () => {
  const messages = [
    { type: 'ai', content: 'the answer' },
    { type: 'tool', content: 'tool output that must never be rendered as Leo speaking' },
    { type: 'human', content: 'continue' },
  ];

  assert.equal(pickAuthoritativeMessage(messages, { partialText: '' }), 'the answer');
});

test('returns null rather than guessing when there is no assistant message', () => {
  assert.equal(pickAuthoritativeMessage([{ type: 'human', content: 'hi' }], {}), null);
  assert.equal(pickAuthoritativeMessage([], {}), null);
  assert.equal(pickAuthoritativeMessage(null, {}), null);
});

test('a late resume from a superseded generation is discarded', () => {
  // The fetch is async and chunks may still be arriving. Without this, a slow response
  // from an older reconnect can overwrite a newer, correct render.
  const gen = nextResumeGeneration(0);
  const newer = nextResumeGeneration(gen);

  assert.equal(isStaleResume(gen, newer), true, 'older generation must be dropped');
  assert.equal(isStaleResume(newer, newer), false, 'current generation must apply');
});

test('resume is wired into the reconnect path, not just defined', () => {
  assert.match(WS_MANAGER, /StreamResume/,
    'WebSocketManager must use the resume module on reconnect');
});

test('the ActionCable close path no longer dead-ends', () => {
  // handleClose() disabled the send button and then returned early for ActionCable, so
  // nothing re-enabled it and reconnect_attempts stayed 0 forever — a dead gauge that
  // told the last investigation the client never even tried.
  // Anchor on the METHOD DEFINITION, not the `this.scheduleReconnect()` call site above it.
  const defIndex = WS_MANAGER.indexOf('\n  scheduleReconnect(');
  assert.ok(defIndex !== -1, 'scheduleReconnect() should still exist');
  const body = WS_MANAGER.slice(defIndex, defIndex + 600);

  assert.doesNotMatch(
    body,
    /if \(this\.isActionCable\) \{\s*return;\s*\}/,
    'the bare ActionCable early return is what left the UI stuck with input disabled',
  );
  assert.match(WS_MANAGER, /noteReconnect/,
    'reconnect attempts must be counted on every transport, or the instrument lies');
});

test('the resume rewrites the existing bubble instead of adding a second one', () => {
  // Appending would leave the truncated bubble on screen above the corrected one, which
  // reads as Leo answering twice — worse than the bug.
  assert.match(RENDERER, /replaceLastAiMessage\(content\)/);
  assert.match(RENDERER, /bubbles\[bubbles\.length - 1\]/,
    'must target the newest assistant bubble');
  assert.match(RENDERER, /if \(target\.getAttribute\('data-raw-content'\) === content\) return true;/,
    'a second resume with the same text must be a no-op');
});

test('resuming clears the streaming buffers so late chunks cannot re-truncate', () => {
  const fn = INDEX_JS.slice(INDEX_JS.indexOf('replaceStreamingMessage(content)'));
  assert.match(fn.slice(0, 300), /streamingState\?\.reset\?\.\(\)/,
    'a chunk arriving after the resume would otherwise append onto the corrected text');
});

test('the thinking indicator is cleared on resync', () => {
  // It kept spinning after the drop in the incident, which is what made the customer
  // believe Leo had died and reload the page.
  const fn = WS_MANAGER.slice(WS_MANAGER.indexOf('async resumeInFlightRun()'));
  assert.match(fn.slice(0, 1200), /hideThinkingIndicator/);
});

test('an idle reconnect does not repaint the thread', () => {
  // Anchor on the definition, not the `this.handleOpen()` call sites above it.
  const fn = WS_MANAGER.slice(WS_MANAGER.indexOf('\n  handleOpen() {'));
  assert.match(fn.slice(0, 900), /if \(this\.runWasInFlightAtClose\)/,
    'resume must be gated on a run actually having been in flight');
});
