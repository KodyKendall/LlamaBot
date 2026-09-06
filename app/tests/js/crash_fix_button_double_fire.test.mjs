// The Rails crash page's "Ask Leo to fix this" button must not send the same
// fix request twice.
//
// A Pro customer (leo-rofme, 2026-09-05) clicked it twice for one crash — GET
// /airspace, ActionView::Template::Error: undefined local variable
// 'area_frame_id' — 74 seconds apart, because nothing told him the first click
// had registered. Two identical user messages, two threads, two messages off a
// daily cap he hit later the same evening.
//
// The guard lives on the receiving side because the button itself is in the
// Leonardo overlay, which does not reach existing boxes on an update.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AutoSendGuard,
  REPEAT_WINDOW_MS,
  crashFingerprint,
} from '../../frontend/chat/utils/autoSendGuard.js';

const crashCommand = (backtrace) => `Leo, the Rails app crashed while running inside the preview iframe.

Your task:
1. Diagnose the exception.

Request: GET /airspace
Exception: ActionView::Template::Error: undefined local variable 'area_frame_id'

Backtrace:
${backtrace}
`;

function guardAt(clock) {
  return new AutoSendGuard({ now: () => clock.t });
}

// ---------------------------------------------------------------------------
// The actual incident
// ---------------------------------------------------------------------------

test('the second click 74 seconds later is dropped', () => {
  const clock = { t: 0 };
  const guard = guardAt(clock);
  const command = crashCommand('app/views/airspace/index.html.erb:12');

  assert.equal(guard.shouldSend(command), true, 'the first click must go through');
  clock.t = 74_000;
  assert.equal(guard.shouldSend(command), false, 'the repeat click must be dropped');
});

test('a differing backtrace is still the same crash', () => {
  // The same failure re-rendered can produce a different backtrace; fingerprinting
  // on it would let every repeat click through.
  const clock = { t: 0 };
  const guard = guardAt(clock);

  guard.shouldSend(crashCommand('app/views/airspace/index.html.erb:12'));
  clock.t = 30_000;
  assert.equal(guard.shouldSend(crashCommand('a/different/frame.rb:99')), false);
});

test('the fingerprint is the request plus the exception, not the backtrace', () => {
  assert.equal(
    crashFingerprint(crashCommand('one.rb:1')),
    crashFingerprint(crashCommand('two.rb:2')),
  );
  assert.match(crashFingerprint(crashCommand('x')), /GET \/airspace/);
  assert.match(crashFingerprint(crashCommand('x')), /area_frame_id/);
});

// ---------------------------------------------------------------------------
// What must still get through
// ---------------------------------------------------------------------------

test('a different crash is not suppressed by an earlier one', () => {
  const clock = { t: 0 };
  const guard = guardAt(clock);

  guard.shouldSend(crashCommand('x'));
  clock.t = 5_000;
  const other = crashCommand('x').replace('/airspace', '/sectors');
  assert.equal(guard.shouldSend(other), true);
});

test('the same exception on a different path is a different crash', () => {
  const clock = { t: 0 };
  const guard = guardAt(clock);

  guard.shouldSend(crashCommand('x'));
  clock.t = 1_000;
  assert.equal(
    guard.shouldSend(crashCommand('x').replace('GET /airspace', 'POST /airspace')),
    true,
  );
});

test('the same crash hours later is a genuine new report', () => {
  const clock = { t: 0 };
  const guard = guardAt(clock);

  guard.shouldSend(crashCommand('x'));
  clock.t = REPEAT_WINDOW_MS + 1;
  assert.equal(guard.shouldSend(crashCommand('x')), true);
});

test('a non-crash auto-send is deduped on its own text', () => {
  const clock = { t: 0 };
  const guard = guardAt(clock);

  assert.equal(guard.shouldSend('Build me a landing page'), true);
  assert.equal(guard.shouldSend('Build me a landing page'), false);
  assert.equal(guard.shouldSend('Build me a contact form'), true);
});

test('an empty command is never sent', () => {
  const guard = new AutoSendGuard();
  assert.equal(guard.shouldSend(''), false);
  assert.equal(guard.shouldSend(undefined), false);
  assert.equal(guard.shouldSend('   '), false);
});

// ---------------------------------------------------------------------------
// Housekeeping
// ---------------------------------------------------------------------------

test('aged-out entries are forgotten rather than accumulating', () => {
  const clock = { t: 0 };
  const guard = guardAt(clock);

  for (let i = 0; i < 50; i++) {
    clock.t = i * 1000;
    guard.shouldSend(`command ${i}`);
  }
  assert.equal(guard.sent.size, 50);

  clock.t += REPEAT_WINDOW_MS;
  guard.shouldSend('one more');
  assert.equal(guard.sent.size, 1, 'a long session must not leak fingerprints');
});
