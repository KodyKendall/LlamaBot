// GitHub device-flow auto-polling (the /gh modal).
//
// The whole point of the device flow is that the user should never have to press
// "check now" — they type the code on github.com and the modal notices. What
// makes that fail in a real browser is not the happy path, it's:
//
//   1. the chat tab is BACKGROUNDED while they authorize on github.com, so
//      timers are throttled to ~1/min — coming back must re-check immediately,
//      not sit on a stale timer;
//   2. the success poll installs the token on the host (docker cp, tens of
//      seconds) — a second poll firing underneath it would ask GitHub about an
//      already-redeemed device code and paint an error over the success;
//   3. one transient 5xx must not end the wait.
//
// Drives the real poller against injected timers/fetch, like overlay_ads.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_INTERVAL,
  DeviceAuthPoller,
  MIN_WATCH_SECONDS,
  POLL_ENDPOINT,
} from '../../frontend/chat/checkpoints/GitHubAuthModal.js';

/** Controllable clock + timers: nothing waits on real time. */
function harness({ replies = [], expiresIn = 900 } = {}) {
  const state = {
    clock: 0,
    pending: null,          // the one scheduled poll
    calls: [],              // fetch bodies
    results: [],            // [status, data]
    listeners: {},
  };

  const timers = {
    setTimeout(fn, ms) { state.pending = { fn, at: state.clock + ms }; return 1; },
    clearTimeout() { state.pending = null; },
  };

  const poller = new DeviceAuthPoller({
    deviceCode: 'DEV-CODE',
    interval: DEFAULT_INTERVAL,
    expiresIn,
    now: () => state.clock,
    timers,
    fetchFn: async (url, opts) => {
      state.calls.push({ url, body: JSON.parse(opts.body) });
      const next = replies.shift() ?? { status: 'pending' };
      if (typeof next === 'function') return next();
      if (next.httpError) return { ok: false, status: 500 };
      return { ok: true, json: async () => next };
    },
    doc: { addEventListener: (n, fn) => { state.listeners[n] = fn; }, removeEventListener: (n) => { delete state.listeners[n]; }, visibilityState: 'visible' },
    win: { addEventListener: (n, fn) => { state.listeners[n] = fn; }, removeEventListener: (n) => { delete state.listeners[n]; } },
    onResult: (status, data) => state.results.push([status, data]),
  });

  /** Advance the clock and run the scheduled poll if it came due. */
  state.advance = async (seconds) => {
    state.clock += seconds * 1000;
    const due = state.pending;
    if (due && due.at <= state.clock) { state.pending = null; await due.fn(); }
  };
  state.wake = async (name = 'visibilitychange') => { await state.listeners[name]?.(); };

  return { poller, state };
}

test('polls on its own — no user click needed', async () => {
  const { poller, state } = harness();
  poller.start();
  assert.equal(state.calls.length, 0, 'no poll before the first interval');

  await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.calls.length, 1);
  assert.equal(state.calls[0].url, POLL_ENDPOINT);
  assert.equal(state.calls[0].body.device_code, 'DEV-CODE');

  await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.calls.length, 2, 'pending keeps the loop going');
});

test('keeps checking for at least two minutes of pending replies', async () => {
  const { poller, state } = harness();
  poller.start();
  const ticks = MIN_WATCH_SECONDS / DEFAULT_INTERVAL;
  for (let i = 0; i < ticks; i++) await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.calls.length, ticks);
  assert.equal(state.results.filter(([s]) => s !== 'pending').length, 0, 'still waiting, not errored out');
});

test('returning to the tab re-checks immediately instead of waiting out a throttled timer', async () => {
  const { poller, state } = harness();
  poller.start();
  await state.advance(DEFAULT_INTERVAL);          // one poll, then backgrounded
  assert.equal(state.calls.length, 1);

  state.clock += 60 * 1000;                        // browser throttled us for a minute
  await state.wake('visibilitychange');            // user comes back from github.com
  assert.equal(state.calls.length, 2, 'checked on wake');
});

test('a wake inside GitHub rate-limit window does not fire an extra poll', async () => {
  const { poller, state } = harness();
  poller.start();
  await state.advance(DEFAULT_INTERVAL);
  state.clock += 1000;                             // only 1s since the last poll
  await state.wake('focus');
  assert.equal(state.calls.length, 1, 'respected the interval floor');
});

test('no second poll while the success install is still running', async () => {
  let release;
  const slowSuccess = () => new Promise((res) => { release = () => res({ ok: true, json: async () => ({ status: 'success', message: 'ok' }) }); });
  const { poller, state } = harness({ replies: [slowSuccess] });
  poller.start();

  const inFlight = state.advance(DEFAULT_INTERVAL);
  state.clock += 30 * 1000;                        // token install drags on
  await state.wake('visibilitychange');
  await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.calls.length, 1, 'nothing polled underneath the in-flight check');

  release();
  await inFlight;
  assert.deepEqual(state.results.map(([s]) => s), ['success']);
  assert.equal(state.pending, null, 'stopped after success');
});

test('a transient server error keeps the wait alive', async () => {
  const { poller, state } = harness({ replies: [{ httpError: true }, { status: 'pending' }, { status: 'success' }] });
  poller.start();
  await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.results.filter(([s]) => s === 'error').length, 0, 'one 5xx is not fatal');
  await state.advance(DEFAULT_INTERVAL);
  await state.advance(DEFAULT_INTERVAL);
  assert.deepEqual(state.results.at(-1)[0], 'success');
});

test('repeated failures surface an error instead of spinning forever', async () => {
  const { poller, state } = harness({ replies: Array.from({ length: 6 }, () => ({ httpError: true })) });
  poller.start();
  for (let i = 0; i < 6; i++) await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.results.at(-1)[0], 'error');
  assert.equal(state.pending, null);
});

test('slow_down backs the interval off and keeps polling', async () => {
  const { poller, state } = harness({ replies: [{ status: 'slow_down', interval: 10 }] });
  poller.start();
  await state.advance(DEFAULT_INTERVAL);
  await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.calls.length, 1, 'waited the longer interval');
  await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.calls.length, 2);
});

test('expired and denied stop the loop', async () => {
  for (const status of ['expired', 'denied']) {
    const { poller, state } = harness({ replies: [{ status }] });
    poller.start();
    await state.advance(DEFAULT_INTERVAL);
    assert.equal(state.results.at(-1)[0], status);
    assert.equal(state.pending, null, `${status} stops polling`);
  }
});

test('gives up once the device code lifetime is over', async () => {
  const { poller, state } = harness({ expiresIn: 30 });
  poller.start();
  for (let i = 0; i < 30; i++) await state.advance(DEFAULT_INTERVAL);
  assert.equal(state.results.at(-1)[0], 'expired');
  assert.equal(state.pending, null);
});

test('close() stops everything and unhooks the wake listeners', async () => {
  const { poller, state } = harness();
  poller.start();
  poller.stop();
  assert.equal(state.pending, null);
  assert.deepEqual(Object.keys(state.listeners), []);
});
