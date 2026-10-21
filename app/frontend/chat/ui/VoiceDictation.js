/**
 * Live voice dictation into the chat box (AssemblyAI Universal-Streaming v3).
 *
 * Click the mic: the browser asks our server for a 60-second streaming token
 * (POST /api/voice-dictation/token — the master key never leaves the server),
 * opens a WebSocket straight to AssemblyAI, and streams 16 kHz PCM16 audio from
 * an AudioWorklet (ui/pcm16-worklet.js). Words stream into the box as you speak;
 * each turn's partial text is replaced by its formatted final. Click again to
 * stop: the last words are flushed, and the message is NOT sent — the user
 * reviews it and presses Send.
 *
 * Dictation goes in at the cursor (or the end, if the box was never focused) and
 * never overwrites what was typed. Only shown when the server says the feature
 * is on (window.LLAMABOT_VOICE_DICTATION) — see app/services/voice_dictation.py.
 */

export const STREAMING_URL = 'wss://streaming.assemblyai.com/v3/ws';

/** How long stop() waits for AssemblyAI to flush the final words. */
const FINALIZE_TIMEOUT_MS = 1200;

/**
 * Put dictated text between the text before and after the cursor, adding a
 * space only where words would otherwise run together.
 */
export function spliceDictation(before, text, after) {
  if (!text) return { value: before + after, caret: before.length };
  const lead = before && !/\s$/.test(before) ? ' ' : '';
  const tail = after && !/^\s/.test(after) ? ' ' : '';
  const head = before + lead + text;
  return { value: head + tail + after, caret: head.length };
}

/** One short, plain sentence for anything that stops dictation from starting. */
export function dictationErrorMessage(err) {
  switch (err && err.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access is blocked. Allow it in your browser to use dictation.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone found.';
    case 'NotReadableError':
      return 'Your microphone is in use by another app.';
    case 'DictationOff':
      return 'Voice dictation is turned off in Settings.';
    case 'DictationUnsupported':
      return "This browser can't record audio here.";
    case 'DictationDropped':
      return 'Dictation stopped — the connection dropped.';
    default:
      return "Voice dictation couldn't start. Try again.";
  }
}

function namedError(name, message = name) {
  return Object.assign(new Error(message), { name });
}

async function fetchStreamingToken() {
  const r = await fetch('/api/voice-dictation/token', { method: 'POST', credentials: 'same-origin' });
  if (r.status === 403) throw namedError('DictationOff');
  if (!r.ok) throw namedError('DictationUnavailable');
  const { token } = await r.json();
  if (!token) throw namedError('DictationUnavailable');
  return token;
}

function browserDeps() {
  return {
    fetchToken: fetchStreamingToken,
    getUserMedia: (constraints) => {
      if (!navigator.mediaDevices?.getUserMedia) throw namedError('DictationUnsupported');
      return navigator.mediaDevices.getUserMedia(constraints);
    },
    createAudioContext: () => {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx || typeof AudioWorkletNode === 'undefined') throw namedError('DictationUnsupported');
      return new Ctx();
    },
    createWorkletNode: (ctx) => new AudioWorkletNode(ctx, 'pcm16-downsampler'),
    createWebSocket: (url) => new WebSocket(url),
    workletUrl: new URL('./pcm16-worklet.js', import.meta.url).href,
    notify: (message) => console.warn('[dictation]', message),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
  };
}

export class VoiceDictation {
  constructor(deps = {}) {
    this.deps = { ...browserDeps(), ...deps };
    this.state = 'idle';     // idle → starting → recording → stopping → idle
    this.session = 0;        // bumped on every start/stop/cancel; stale async work checks it
    this.turns = new Map();  // turn_order → latest transcript
    this.dropped = new Set();
    this.before = '';
    this.after = '';
    this.lastWritten = null;
    this.inputHadFocus = false;
  }

  init(button, input) {
    this.button = button;
    this.input = input;
    if (!button || !input) return;
    button.classList.remove('hidden');
    this._paint();

    // Note where the cursor was BEFORE the click moves focus to the button, and
    // (mouse) keep focus in the box so the caret stays visible while dictating.
    button.addEventListener('pointerdown', () => {
      this.inputHadFocus = document.activeElement === input;
    });
    button.addEventListener('mousedown', (e) => e.preventDefault());
    button.addEventListener('click', (e) => {
      e.preventDefault();
      this.toggle();
    });
  }

  toggle() {
    if (this.state === 'idle') this.start();
    else this.stop();
  }

  async start() {
    if (this.state !== 'idle') return;
    const session = ++this.session;
    this._setState('starting');
    this._anchor();

    // Created synchronously inside the click: Safari only lets audio start
    // from a user gesture, and every await below gives that up.
    let ctx;
    try {
      ctx = this.deps.createAudioContext();
      this.ctx = ctx;
      ctx.resume?.();
    } catch (err) {
      return this._fail(err, session);
    }
    // A start-up that was stopped (and maybe restarted) while it awaited must
    // release only what IT acquired — never the newer session's audio.
    const abandon = (stream) => {
      stream?.getTracks().forEach((t) => t.stop());
      ctx.close?.().catch?.(() => {});
    };

    const [tokenResult, streamResult] = await Promise.allSettled([
      this.deps.fetchToken(),
      Promise.resolve().then(() => this.deps.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      })),
    ]);
    const stream = streamResult.status === 'fulfilled' ? streamResult.value : null;
    if (session !== this.session) return abandon(stream);
    this.stream = stream;
    // Microphone problems first — they are the ones the user can fix.
    if (streamResult.status === 'rejected') return this._fail(streamResult.reason, session);
    if (tokenResult.status === 'rejected') return this._fail(tokenResult.reason, session);

    try {
      await ctx.audioWorklet.addModule(this.deps.workletUrl);
      if (session !== this.session) return abandon(stream);
      this.source = this.ctx.createMediaStreamSource(this.stream);
      this.node = this.deps.createWorkletNode(this.ctx);
    } catch (err) {
      return this._fail(err, session);
    }

    const params = new URLSearchParams({ sample_rate: '16000', format_turns: 'true', token: tokenResult.value });
    const ws = this.deps.createWebSocket(`${STREAMING_URL}?${params}`);
    this.ws = ws;
    ws.binaryType = 'arraybuffer';
    ws.onmessage = (e) => { if (session === this.session) this._onMessage(e.data); };
    ws.onerror = () => {};   // onclose always follows and carries the decision
    ws.onclose = () => { if (session === this.session) this._onClose(); };
    ws.onopen = () => {
      if (session !== this.session || this.state !== 'starting') return;
      this.node.port.onmessage = (e) => {
        if (this.state === 'recording' && ws.readyState === 1) ws.send(e.data);
      };
      this.source.connect(this.node);
      // Outputs silence; connecting it keeps every browser pulling audio through.
      this.node.connect(this.ctx.destination);
      this._setState('recording');
    };
  }

  /** Stop listening, let AssemblyAI flush the last words, then close. Never sends. */
  async stop() {
    if (this.state === 'starting') {
      this.session++;
      this._teardown();
      return;
    }
    if (this.state !== 'recording') return;
    const session = this.session;
    this._setState('stopping');
    this._stopAudio();
    try { this.ws.send(JSON.stringify({ type: 'Terminate' })); } catch { /* already closing */ }

    await new Promise((resolve) => {
      this._terminated = resolve;
      this._finalizeTimer = this.deps.setTimeout(resolve, FINALIZE_TIMEOUT_MS);
    });
    this.deps.clearTimeout(this._finalizeTimer);
    if (session === this.session) {
      this.session++;
      this._teardown();
    }
  }

  /** The message just went out: tear down now and ignore anything still in flight. */
  cancel() {
    if (this.state === 'idle') return;
    this.session++;
    this._terminated?.();
    this._teardown();
  }

  // ==========================================================================
  // Transcripts
  // ==========================================================================

  _onMessage(data) {
    if (typeof data !== 'string') return;
    let msg;
    try { msg = JSON.parse(data); } catch { return; }

    if (msg.type === 'Turn') this._onTurn(msg);
    else if (msg.type === 'Termination') this._terminated?.();
  }

  _onTurn(msg) {
    const order = msg.turn_order;
    if (this.dropped.has(order)) return;
    if (this.input.value !== this.lastWritten) this._reanchor(order);
    if (this.dropped.has(order)) return;
    this.turns.set(order, (msg.transcript || '').trim());
    this._render();
  }

  _render() {
    const text = [...this.turns.keys()]
      .sort((a, b) => a - b)
      .map((k) => this.turns.get(k))
      .filter(Boolean)
      .join(' ');
    const { value, caret } = spliceDictation(this.before, text, this.after);
    this.input.value = value;
    this.lastWritten = value;
    try { this.input.setSelectionRange(caret, caret); } catch { /* not focusable right now */ }
    this.input.dispatchEvent(new Event('input', { bubbles: true }));
  }

  /** Where dictation goes: the cursor if the box had focus, else the end. */
  _anchor() {
    const { value } = this.input;
    const at = this.inputHadFocus || document.activeElement === this.input
      ? (this.input.selectionEnd ?? value.length)
      : value.length;
    this.before = value.slice(0, at);
    this.after = value.slice(at);
    this.turns = new Map();
    this.dropped = new Set();
    this.lastWritten = value;
    this.inputHadFocus = false;
  }

  /**
   * The user edited the box mid-dictation. Everything already in it is theirs
   * now; keep going from where they left the cursor. The turn still being
   * spoken carries on in place if its words are still there unchanged.
   */
  _reanchor(liveOrder) {
    const value = this.input.value;
    const live = this.turns.get(liveOrder);
    const at = live ? value.lastIndexOf(live) : -1;

    for (const k of this.turns.keys()) if (k !== liveOrder || at < 0) this.dropped.add(k);
    if (at >= 0) {
      this.before = value.slice(0, at);
      this.after = value.slice(at + live.length);
      this.turns = new Map([[liveOrder, live]]);
    } else {
      const caret = this.input.selectionEnd ?? value.length;
      this.before = value.slice(0, caret);
      this.after = value.slice(caret);
      this.turns = new Map();
    }
    this.lastWritten = value;
  }

  // ==========================================================================
  // Lifecycle plumbing
  // ==========================================================================

  _onClose() {
    if (this.state === 'stopping') { this._terminated?.(); return; }
    // Closed under us while live (network, expired session, server error).
    // Whatever was transcribed stays in the box.
    const wasLive = this.state === 'recording' || this.state === 'starting';
    this.session++;
    this._teardown();
    if (wasLive) this.deps.notify(dictationErrorMessage(namedError('DictationDropped')));
  }

  _fail(err, session) {
    if (session !== this.session) return;
    this.session++;
    this._teardown();
    this.deps.notify(dictationErrorMessage(err));
  }

  _stopAudio() {
    if (this.node) {
      this.node.port.onmessage = null;
      try { this.node.disconnect(); } catch { /* never connected */ }
    }
    try { this.source?.disconnect(); } catch { /* never connected */ }
    this.stream?.getTracks().forEach((t) => t.stop());
    this.ctx?.close?.().catch?.(() => {});
    this.node = this.source = this.stream = this.ctx = null;
  }

  _teardown() {
    this._stopAudio();
    if (this.ws) {
      this.ws.onmessage = this.ws.onclose = this.ws.onopen = null;
      try { this.ws.close(); } catch { /* already closed */ }
      this.ws = null;
    }
    this._terminated = null;
    this._setState('idle');
  }

  _setState(state) {
    this.state = state;
    this._paint();
  }

  _paint() {
    if (!this.button) return;
    const live = this.state === 'recording' || this.state === 'stopping';
    this.button.classList.toggle('recording', live);
    this.button.classList.toggle('starting', this.state === 'starting');
    this.button.setAttribute('aria-pressed', live ? 'true' : 'false');
    const label = this.state === 'idle' ? 'Start dictation' : 'Stop dictation';
    this.button.setAttribute('aria-label', label);
    this.button.dataset.tooltip = this.state === 'idle' ? 'Dictate — click again to stop' : 'Stop dictation';
  }
}
