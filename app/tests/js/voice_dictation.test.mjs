// Tests for live voice dictation (ui/VoiceDictation.js + ui/pcm16-worklet.js).
//
// The mic button streams speech into the chat box through AssemblyAI. What has
// to hold: dictated text lands at the cursor (or the end) without touching what
// was already typed, partial words are replaced by the formatted final turn,
// stopping never sends the message, a blocked microphone resets cleanly with a
// plain message, and the audio leaves as 16 kHz PCM16 in ~50 ms chunks.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  VoiceDictation,
  spliceDictation,
  dictationErrorMessage,
  STREAMING_URL,
} from '../../frontend/chat/ui/VoiceDictation.js';

// ---------------------------------------------------------------------------
// Fakes
// ---------------------------------------------------------------------------

class FakeClassList {
  constructor() { this._set = new Set(); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
  toggle(n, on) { on ? this.add(n) : this.remove(n); }
}

class FakeElement {
  constructor() {
    this.classList = new FakeClassList();
    this.attrs = {};
    this.dataset = {};
    this.handlers = {};
    this.value = '';
    this.selectionStart = 0;
    this.selectionEnd = 0;
    this.events = [];
  }
  addEventListener(t, fn) { (this.handlers[t] ||= []).push(fn); }
  fire(t, ev = {}) { (this.handlers[t] || []).forEach((fn) => fn({ preventDefault() {}, ...ev })); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  dispatchEvent(ev) { this.events.push(ev.type); return true; }
}

class FakeSocket {
  constructor(url) {
    this.url = url;
    this.sent = [];
    this.readyState = 0;
    this.closed = false;
  }
  open() { this.readyState = 1; this.onopen?.({}); }
  send(d) { this.sent.push(d); }
  close() { this.closed = true; this.readyState = 3; }
  receive(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
  serverClose(code = 1011) { this.readyState = 3; this.onclose?.({ code }); }
}

function harness({ gum, token, typed = '', focusAt = null } = {}) {
  globalThis.Event = class { constructor(type) { this.type = type; } };
  const input = new FakeElement();
  input.value = typed;
  const button = new FakeElement();
  button.classList.add('hidden');
  globalThis.document = { activeElement: null };
  if (focusAt !== null) {
    globalThis.document.activeElement = input;
    input.selectionStart = input.selectionEnd = focusAt;
  }

  const sockets = [];
  const notices = [];
  const tracks = [{ stopped: false, stop() { this.stopped = true; } }];
  const stream = { getTracks: () => tracks };
  const ctx = {
    closed: false,
    resume: async () => {},
    close: async () => { ctx.closed = true; },
    audioWorklet: { addModule: async () => {} },
    createMediaStreamSource: () => ({ connect() {}, disconnect() {} }),
    destination: {},
  };
  const node = { port: {}, connect() {}, disconnect() {} };
  const timers = [];

  const vd = new VoiceDictation({
    fetchToken: token || (async () => 'temp-token'),
    getUserMedia: gum || (async () => stream),
    createAudioContext: () => ctx,
    createWorkletNode: () => node,
    createWebSocket: (url) => { const s = new FakeSocket(url); sockets.push(s); return s; },
    notify: (m) => notices.push(m),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
  });
  vd.init(button, input);
  return { vd, input, button, sockets, notices, tracks, ctx, node, timers };
}

/** Click the mic and let the async start-up run until the socket exists, then open it. */
async function startRecording(h) {
  h.button.fire('pointerdown');
  h.button.fire('click');
  for (let i = 0; i < 10 && h.sockets.length === 0; i++) await new Promise((r) => setImmediate(r));
  h.sockets[0]?.open();
  await new Promise((r) => setImmediate(r));
}

const turn = (turn_order, transcript, extra = {}) =>
  ({ type: 'Turn', turn_order, transcript, end_of_turn: false, turn_is_formatted: false, ...extra });

// ---------------------------------------------------------------------------
// Text insertion
// ---------------------------------------------------------------------------

test('splice keeps text on both sides and adds a space where words would touch', () => {
  assert.deepEqual(spliceDictation('Hello', 'big', 'world'), { value: 'Hello big world', caret: 9 });
  assert.deepEqual(spliceDictation('Context: ', 'Add a model', ''), { value: 'Context: Add a model', caret: 20 });
  assert.deepEqual(spliceDictation('', 'hi', ''), { value: 'hi', caret: 2 });
  // Nothing said yet: the box is exactly what was typed.
  assert.deepEqual(spliceDictation('Hello', '', ' world'), { value: 'Hello world', caret: 5 });
});

test('partials stream in after typed text and the formatted final replaces them', async () => {
  const h = harness({ typed: 'Context: ', focusAt: 9 });
  await startRecording(h);
  const ws = h.sockets[0];

  ws.receive(turn(0, 'add a rails'));
  assert.equal(h.input.value, 'Context: add a rails');
  ws.receive(turn(0, 'add a rails model called invoice in postgres', { end_of_turn: true }));
  ws.receive(turn(0, 'Add a Rails model called Invoice in PostgreSQL.', { end_of_turn: true, turn_is_formatted: true }));
  assert.equal(h.input.value, 'Context: Add a Rails model called Invoice in PostgreSQL.');

  ws.receive(turn(1, 'then add'));
  assert.equal(h.input.value, 'Context: Add a Rails model called Invoice in PostgreSQL. then add');
  assert.ok(h.input.events.includes('input'), 'send button / autosize must hear about the change');
});

test('dictation goes in at the cursor, not over the text after it', async () => {
  const h = harness({ typed: 'Hello world', focusAt: 5 });
  await startRecording(h);
  h.sockets[0].receive(turn(0, 'big'));

  assert.equal(h.input.value, 'Hello big world');
  assert.equal(h.input.selectionStart, 9);
});

test('an unfocused box gets dictation appended at the end', async () => {
  const h = harness({ typed: 'Hello world' });
  h.input.selectionStart = h.input.selectionEnd = 0;   // stale caret from earlier
  await startRecording(h);
  h.sockets[0].receive(turn(0, 'again'));

  assert.equal(h.input.value, 'Hello world again');
});

test('typing while dictating keeps the edit and carries on from the cursor', async () => {
  const h = harness({ typed: '', focusAt: 0 });
  await startRecording(h);
  const ws = h.sockets[0];
  ws.receive(turn(0, 'First sentence.', { end_of_turn: true, turn_is_formatted: true }));
  ws.receive(turn(1, 'second'));

  // The user fixes the first sentence by hand, cursor at the end.
  h.input.value = 'First sentence, edited. second';
  h.input.selectionStart = h.input.selectionEnd = h.input.value.length;
  ws.receive(turn(1, 'second part'));

  assert.equal(h.input.value, 'First sentence, edited. second part');
});

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

test('connects to the v3 streaming URL with the temporary token', async () => {
  const h = harness();
  await startRecording(h);
  const url = new URL(h.sockets[0].url);

  assert.equal(`${url.origin}${url.pathname}`, STREAMING_URL);
  assert.equal(url.searchParams.get('sample_rate'), '16000');
  assert.equal(url.searchParams.get('format_turns'), 'true');
  assert.equal(url.searchParams.get('token'), 'temp-token');
});

test('the button shows recording while live and goes back after stop', async () => {
  const h = harness();
  assert.equal(h.button.classList.contains('hidden'), false, 'init reveals the button');

  await startRecording(h);
  assert.equal(h.button.classList.contains('recording'), true);
  assert.equal(h.button.getAttribute('aria-pressed'), 'true');

  h.button.fire('click');
  h.sockets[0].receive({ type: 'Termination' });
  await new Promise((r) => setImmediate(r));
  assert.equal(h.button.classList.contains('recording'), false);
  assert.equal(h.button.getAttribute('aria-pressed'), 'false');
});

test('stop sends Terminate, keeps the last words, closes, and never sends the message', async () => {
  const h = harness({ typed: 'Context: ', focusAt: 9 });
  await startRecording(h);
  const ws = h.sockets[0];
  ws.receive(turn(0, 'add a rails model'));

  h.button.fire('click');
  assert.deepEqual(JSON.parse(ws.sent.at(-1)), { type: 'Terminate' });
  assert.equal(h.tracks[0].stopped, true, 'the microphone is released at once');

  // AssemblyAI flushes the final turn after Terminate.
  ws.receive(turn(0, 'Add a Rails model.', { end_of_turn: true, turn_is_formatted: true }));
  ws.receive({ type: 'Termination' });
  await new Promise((r) => setImmediate(r));

  assert.equal(h.input.value, 'Context: Add a Rails model.');
  assert.equal(ws.closed, true);
  assert.equal(h.ctx.closed, true);
  assert.deepEqual(h.input.events.filter((e) => e !== 'input'), [], 'no submit/keydown — the user presses Send');
});

test('stop gives up waiting for the final words after about a second', async () => {
  const h = harness();
  await startRecording(h);
  h.button.fire('click');

  const wait = h.timers.at(-1);
  assert.ok(wait.ms <= 1500, `finalize timeout is ${wait.ms}ms`);
  wait.fn();
  await new Promise((r) => setImmediate(r));
  assert.equal(h.sockets[0].closed, true);
  assert.equal(h.button.classList.contains('recording'), false);
});

test('audio chunks go to the socket only while recording', async () => {
  const h = harness();
  await startRecording(h);
  const chunk = new ArrayBuffer(1600);
  h.node.port.onmessage({ data: chunk });
  assert.equal(h.sockets[0].sent[0], chunk);
});

test('a blocked microphone says so plainly and resets', async () => {
  const denied = Object.assign(new Error('Permission denied'), { name: 'NotAllowedError' });
  const h = harness({ gum: async () => { throw denied; } });
  await startRecording(h);

  assert.equal(h.notices.length, 1);
  assert.match(h.notices[0], /microphone access is blocked/i);
  assert.equal(h.sockets.length, 0, 'never opens a stream without audio');
  assert.equal(h.button.classList.contains('recording'), false);
  assert.equal(h.ctx.closed, true);

  // And the button works again afterwards.
  await startRecording(harness());
});

test('a token failure releases the microphone that was already granted', async () => {
  const h = harness({ token: async () => { throw Object.assign(new Error('x'), { name: 'DictationUnavailable' }); } });
  await startRecording(h);

  assert.equal(h.tracks[0].stopped, true);
  assert.equal(h.notices.length, 1);
  assert.equal(h.button.classList.contains('recording'), false);
});

test('the stream dropping mid-dictation keeps the text and resets', async () => {
  const h = harness();
  await startRecording(h);
  h.sockets[0].receive(turn(0, 'keep me'));
  h.sockets[0].serverClose(1011);

  assert.equal(h.input.value, 'keep me');
  assert.equal(h.notices.length, 1);
  assert.equal(h.button.classList.contains('recording'), false);
  assert.equal(h.tracks[0].stopped, true);
});

test('cancel (the message was sent) drops late words instead of refilling the box', async () => {
  const h = harness();
  await startRecording(h);
  const ws = h.sockets[0];
  ws.receive(turn(0, 'sent already'));

  h.input.value = '';          // index.js cleared the box on send
  h.vd.cancel();
  ws.receive(turn(0, 'sent already.', { end_of_turn: true, turn_is_formatted: true }));

  assert.equal(h.input.value, '');
  assert.equal(ws.closed, true);
  assert.equal(h.button.classList.contains('recording'), false);
});

test('error messages are short and plain', () => {
  assert.match(dictationErrorMessage({ name: 'NotAllowedError' }), /blocked/i);
  assert.match(dictationErrorMessage({ name: 'NotFoundError' }), /no microphone/i);
  assert.match(dictationErrorMessage({ name: 'DictationOff' }), /settings/i);
  for (const name of ['NotAllowedError', 'NotFoundError', 'DictationOff', 'TypeError', undefined]) {
    assert.ok(dictationErrorMessage({ name }).length < 120);
  }
});

// ---------------------------------------------------------------------------
// The AudioWorklet: 16 kHz mono PCM16, ~50 ms per chunk
// ---------------------------------------------------------------------------

test('the worklet downsamples 48 kHz to 16 kHz PCM16 in 50 ms chunks', async () => {
  let Processor;
  globalThis.sampleRate = 48000;
  globalThis.AudioWorkletProcessor = class { constructor() { this.port = { posted: [], postMessage(d) { this.posted.push(d); } }; } };
  globalThis.registerProcessor = (name, cls) => { assert.equal(name, 'pcm16-downsampler'); Processor = cls; };
  await import('../../frontend/chat/ui/pcm16-worklet.js');

  const p = new Processor();
  const block = new Float32Array(128).fill(0.5);
  // 50 ms at 48 kHz = 2400 input samples ≈ 19 render quanta.
  for (let i = 0; i < 19; i++) assert.equal(p.process([[block]]), true);

  assert.equal(p.port.posted.length, 1);
  const pcm = new Int16Array(p.port.posted[0]);
  assert.equal(pcm.length, 800);                     // 50 ms at 16 kHz
  assert.equal(pcm[0], Math.round(0.5 * 0x7fff));

  // Clipping input is clamped, not wrapped around.
  const loud = new Float32Array(128).fill(4);
  for (let i = 0; i < 19; i++) p.process([[loud]]);
  const last = new Int16Array(p.port.posted.at(-1));
  assert.equal(Math.max(...last), 0x7fff);

  // No input (mic muted / not yet connected) is not an error.
  assert.equal(p.process([[]]), true);
});

test('stopping during start-up, then starting again, leaves the new session alone', async () => {
  let releaseFirst;
  const firstTracks = [{ stopped: false, stop() { this.stopped = true; } }];
  let calls = 0;
  const h = harness({
    gum: () => {
      calls += 1;
      if (calls === 1) return new Promise((r) => { releaseFirst = () => r({ getTracks: () => firstTracks }); });
      return Promise.resolve({ getTracks: () => h.tracks });
    },
  });
  h.button.fire('click');              // start #1 — stuck waiting for the mic prompt
  await new Promise((r) => setImmediate(r));
  h.button.fire('click');              // stop while starting
  await startRecording(h);             // start #2 goes live
  assert.equal(h.button.classList.contains('recording'), true);

  releaseFirst();                      // the first prompt finally resolves
  await new Promise((r) => setImmediate(r));

  assert.equal(firstTracks[0].stopped, true, 'the abandoned mic is released');
  assert.equal(h.tracks[0].stopped, false, 'the live session keeps its mic');
  assert.equal(h.button.classList.contains('recording'), true);
});
