// Tests for the Rails crash poller behind the chat error tray
// (ui/RailsErrorPoll.js).
//
// The tray already shows JavaScript errors the preview pushes over postMessage.
// Server-side crashes cannot be pushed — the Rails app has no handle on the
// chat page — so LlamaBot proxies the gem's crash feed and this class polls it.
//
// The properties that matter, in the order a user would notice them breaking:
// the tray arms itself on load and never shows crashes that predate the session;
// a repeat poll never re-reports what it already showed; a Rails restart (which
// rewinds the feed's sequence to zero) does not wedge the poller forever; while
// Leo is mid-turn the errors are Leo's to fix, not the user's to read; and no
// failure anywhere — no token, dead feed, garbage body — can throw into the page.

import assert from 'node:assert/strict';
import test from 'node:test';

import { RailsErrorPoll } from '../../frontend/chat/ui/RailsErrorPoll.js';

const ERROR = {
  id: 'rails-5',
  kind: 'rails',
  message: 'NoMethodError: undefined method `title\' for nil',
  path: 'GET /posts/1',
  count: 1,
  stack: 'app/views/posts/show.html.erb:3',
};

/**
 * @param {Array} responses - one entry per tick; a body, or an Error to throw.
 */
function harness(responses, opts = {}) {
  const calls = [];
  const recorded = [];
  let running = false;

  const poll = new RailsErrorPoll({
    getToken: opts.getToken || (async () => 'tok-abc'),
    isAgentRunning: () => running,
    onErrors: (errors) => recorded.push(...errors),
    isVisible: opts.isVisible || (() => true),
    fetchImpl: async (url, init) => {
      calls.push({ url, headers: (init && init.headers) || {} });
      const next = responses.shift();
      if (next instanceof Error) throw next;
      if (next && next.__http) return { ok: false, status: next.__http };
      return { ok: true, status: 200, json: async () => next };
    },
  });

  return {
    poll,
    calls,
    recorded,
    setRunning: (v) => { running = v; },
    since: (i) => new URL(calls[i].url, 'https://box.example').searchParams.get('since'),
  };
}

const body = (seq, errors = [], available = true) => ({ seq, errors, available });

// ---------------------------------------------------------------------------
// Arming
// ---------------------------------------------------------------------------

test('the first poll is a cursor probe and shows nothing', async () => {
  const h = harness([body(9, [ERROR])]);
  await h.poll.tick();

  assert.equal(h.since(0), null, 'no since= on the probe');
  assert.deepEqual(h.recorded, [], 'crashes from before the page opened are not news');
});

test('later polls ask for what came after the probe', async () => {
  const h = harness([body(9), body(10, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();

  assert.equal(h.since(1), '9');
  assert.deepEqual(h.recorded, [ERROR]);
});

test('the cursor advances so an error is reported once', async () => {
  const h = harness([body(9), body(10, [ERROR]), body(10, [])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();

  assert.equal(h.since(2), '10');
  assert.equal(h.recorded.length, 1);
});

// ---------------------------------------------------------------------------
// Rails restarting
// ---------------------------------------------------------------------------

test('a feed that rewound is re-armed instead of going deaf', async () => {
  // Rails restarts, its in-memory ring resets to seq 0. Holding a cursor of 10
  // would mean asking for "everything after 10" forever — the tray would never
  // show another error for the rest of the session.
  const h = harness([body(9), body(10, [ERROR]), body(2, []), body(3, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();   // sees seq go backwards; re-arms at 2, takes nothing
  await h.poll.tick();

  assert.equal(h.since(3), '2');
  assert.equal(h.recorded.length, 2);
});

test('a rewind does not replay the entries in that response', async () => {
  const h = harness([body(9), body(10, [ERROR]), body(2, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();

  assert.equal(h.recorded.length, 1, 'the post-restart page is somebody else’s history');
});

// ---------------------------------------------------------------------------
// Staying out of Leo's way
// ---------------------------------------------------------------------------

test('crashes during an agent turn are left to Leo', async () => {
  // Leo already polls this feed mid-turn and repairs what it broke, without the
  // user ever seeing the broken page. Popping a notice at the same time would
  // hand the user a problem that is already being fixed.
  const h = harness([body(9), body(10, [ERROR])]);
  await h.poll.tick();
  h.setRunning(true);
  await h.poll.tick();

  assert.deepEqual(h.recorded, []);
});

test('the cursor still moves during a turn, so the errors do not surface later', async () => {
  const h = harness([body(9), body(10, [ERROR]), body(10, [])]);
  await h.poll.tick();
  h.setRunning(true);
  await h.poll.tick();
  h.setRunning(false);
  await h.poll.tick();

  assert.equal(h.since(2), '10');
  assert.deepEqual(h.recorded, []);
});

// ---------------------------------------------------------------------------
// Degrading quietly
// ---------------------------------------------------------------------------

test('no Rails token means no request at all', async () => {
  const h = harness([body(9)], { getToken: async () => null });
  await h.poll.tick();

  assert.equal(h.calls.length, 0);
});

test('the token travels in a header, never the query string', async () => {
  const h = harness([body(9)]);
  await h.poll.tick();

  assert.equal(h.calls[0].headers['X-Rails-Api-Token'], 'tok-abc');
  assert.ok(!h.calls[0].url.includes('tok-abc'));
});

test('an unavailable feed holds the cursor rather than losing its place', async () => {
  // A gem too old to serve the endpoint, an expired token, Rails mid-restart.
  const h = harness([body(9), body(null, [], false), body(10, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();

  assert.equal(h.since(2), '9');
  assert.deepEqual(h.recorded, [ERROR]);
});

test('a feed that never answers backs off instead of hammering', async () => {
  const h = harness([body(9), ...Array(5).fill(body(null, [], false))]);
  await h.poll.tick();
  const fast = h.poll.intervalMs;
  for (let i = 0; i < 5; i += 1) await h.poll.tick();

  assert.ok(h.poll.intervalMs > fast, 'a box on an old gem should not poll every few seconds');
});

test('the interval recovers as soon as the feed answers again', async () => {
  const h = harness([body(9), ...Array(5).fill(body(null, [], false)), body(9)]);
  await h.poll.tick();
  const fast = h.poll.intervalMs;
  for (let i = 0; i < 6; i += 1) await h.poll.tick();

  assert.equal(h.poll.intervalMs, fast);
});

test('a thrown fetch is not a broken page', async () => {
  const h = harness([body(9), new Error('offline'), body(10, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();

  assert.deepEqual(h.recorded, [ERROR]);
});

test('an HTTP error is not a broken page', async () => {
  const h = harness([body(9), { __http: 500 }, body(10, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();

  assert.deepEqual(h.recorded, [ERROR]);
});

test('a garbled body is ignored', async () => {
  const h = harness([body(9), { seq: 'not-a-number', errors: 'nope' }, body(10, [ERROR])]);
  await h.poll.tick();
  await h.poll.tick();
  await h.poll.tick();

  assert.equal(h.since(2), '9', 'the bad seq did not become the cursor');
  assert.deepEqual(h.recorded, [ERROR]);
});

// ---------------------------------------------------------------------------
// Not doing work nobody asked for
// ---------------------------------------------------------------------------

test('a hidden tab does not poll', async () => {
  const h = harness([body(9), body(10, [ERROR])], { isVisible: () => false });
  await h.poll.tick();

  assert.equal(h.calls.length, 0);
});

test('a slow poll does not stack up behind itself', async () => {
  let release;
  const gate = new Promise((r) => { release = r; });
  const calls = [];
  const poll = new RailsErrorPoll({
    getToken: async () => 'tok',
    onErrors: () => {},
    fetchImpl: async (url) => {
      calls.push(url);
      await gate;
      return { ok: true, status: 200, json: async () => body(9) };
    },
  });

  const first = poll.tick();
  await poll.tick();          // arrives while the first is still in flight
  release();
  await first;

  assert.equal(calls.length, 1);
});

// ---------------------------------------------------------------------------
// The seam: poller -> tray
// ---------------------------------------------------------------------------
//
// index.js wires these two together with one line
// (`onErrors: (errors) => errors.forEach(e => this.errorAttach.record(e))`), so
// the shape the endpoint returns has to be the shape the tray records. This
// pins that against a real captured response — a live `GET /telemetry_boom`
// against the dev box on 2026-08-23.

test('a real endpoint response lands in the tray as one notice', async () => {
  const { ErrorAttach } = await import('../../frontend/chat/ui/ErrorAttach.js');

  class El {
    constructor() { this.innerHTML = ''; this.children = []; this._c = new Set(); }
    get classList() {
      return { add: (n) => this._c.add(n), remove: (n) => this._c.delete(n), contains: (n) => this._c.has(n) };
    }
    addEventListener() {}
    querySelectorAll() { return []; }
    appendChild(e) { this.children.push(e); return e; }
  }
  globalThis.window = { addEventListener() {} };
  globalThis.document = { createElement: () => new El(), body: new El(), addEventListener() {}, removeEventListener() {} };

  const banner = new El();
  const attach = new ErrorAttach({ getAllowedOrigin: () => 'https://rails.example' });
  attach.init(banner, new El());

  const captured = {
    seq: 2,
    available: true,
    errors: [{
      id: 'rails-2',
      kind: 'rails',
      message: 'RuntimeError: Telemetry smoke test: deliberate crash',
      path: 'GET /telemetry_boom',
      count: 2,
      stack: "app/controllers/telemetry_boom_controller.rb:6:in `show'",
    }],
  };

  const poll = new RailsErrorPoll({
    getToken: async () => 'tok',
    onErrors: (errors) => errors.forEach((e) => attach.record(e)),
    fetchImpl: async () => ({ ok: true, status: 200, json: async () => captured }),
  });

  poll.cursor = 1;            // already armed
  await poll.tick();

  // One broken route is one problem, however many times it fired; the "2" lives
  // on the entry in the popup, not in the notice.
  assert.match(banner.innerHTML, /1 Rails error detected/);
  assert.equal(attach.errors[0].count, 2);
  assert.match(attach.buildMessageBlock(), /<RAILS_SERVER_ERRORS>[\s\S]*telemetry_boom_controller/);
});
