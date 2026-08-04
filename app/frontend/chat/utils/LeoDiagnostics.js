/**
 * Bounded browser-side diagnostics ring buffer for thumbs-down feedback.
 *
 * Lohman's 2026-07-06 thumbs-down said "Seem like after every ticket is created, the
 * connection is lost". The feedback row carried only thread_id/rating/scope/note — no
 * websocket close code, no console output, no model/mode, nothing to distinguish a
 * network drop from a hung run. Support had a complaint and no evidence.
 *
 * This captures just enough to triage that, with hard bounds:
 *  - at most MAX_EVENTS entries, and nothing older than MAX_AGE_MS
 *  - every string truncated
 *  - token-like strings redacted before they are ever stored
 *
 * Deliberately NOT captured: cookies, full HTML, attached file contents, message
 * bodies. This rides along with user-submitted feedback to the mothership.
 */

const MAX_EVENTS = 100;
const MAX_AGE_MS = 5 * 60 * 1000;      // 5 minutes
const MAX_STRING = 300;
const MAX_MESSAGE = 500;

/**
 * Redact anything that looks like a credential. Deliberately aggressive — a missed
 * console.log of a token would ship it to the mothership inside a feedback row.
 */
export function redact(value) {
  if (value == null) return value;
  let s = String(value);
  s = s
    // Bearer / api-key style headers and assignments
    .replace(/\b(bearer|token|api[_-]?key|secret|password|passwd|authorization)\b\s*[:=]\s*\S+/gi,
             (m) => `${m.split(/[:=]/)[0]}=[REDACTED]`)
    // sk-…, lp_…, ghp_… style opaque keys
    .replace(/\b(sk|lp|ghp|gho|glpat|xox[baprs])[-_][A-Za-z0-9_-]{8,}/g, '[REDACTED]')
    // JWTs
    .replace(/\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b/g, '[REDACTED]')
    // long hex blobs (session ids, hashes)
    .replace(/\b[0-9a-f]{32,}\b/gi, '[REDACTED]');
  return s;
}

function trunc(value, max = MAX_STRING) {
  const s = redact(value);
  return s == null ? s : (s.length > max ? `${s.slice(0, max)}…` : s);
}

export class LeoDiagnostics {
  constructor({ now = () => Date.now(), maxEvents = MAX_EVENTS, maxAgeMs = MAX_AGE_MS } = {}) {
    this._now = now;
    this._maxEvents = maxEvents;
    this._maxAgeMs = maxAgeMs;
    this._events = [];
    this.connection = {
      ready_state: null,
      last_close: null,
      last_connected_at: null,
      last_disconnected_at: null,
      reconnect_attempts: 0,
      outbox_length: 0,
    };
    this._consolePatched = false;
  }

  /** Record one bounded, redacted event. */
  record(event, data = {}) {
    try {
      const entry = { at: new Date(this._now()).toISOString(), event: String(event).slice(0, 60) };
      for (const [k, v] of Object.entries(data)) {
        if (v == null) continue;
        entry[k] = (typeof v === 'object') ? trunc(JSON.stringify(v)) : trunc(v);
      }
      this._events.push(entry);
      this._prune();
    } catch (_e) {
      // diagnostics must never break the page
    }
  }

  _prune() {
    const cutoff = this._now() - this._maxAgeMs;
    // Both bounds apply: whichever is smaller wins.
    this._events = this._events.filter((e) => Date.parse(e.at) >= cutoff);
    if (this._events.length > this._maxEvents) {
      this._events = this._events.slice(-this._maxEvents);
    }
  }

  /** Track websocket lifecycle. Called from WebSocketManager. */
  noteConnectionState(patch = {}) {
    Object.assign(this.connection, patch);
  }

  noteOpen(readyState) {
    this.connection.ready_state = readyState;
    this.connection.last_connected_at = new Date(this._now()).toISOString();
    this.connection.reconnect_attempts = 0;
    this.record('websocket_open', { ready_state: readyState });
  }

  noteClose({ code, reason, wasClean, readyState } = {}) {
    const at = new Date(this._now()).toISOString();
    this.connection.ready_state = readyState ?? this.connection.ready_state;
    this.connection.last_disconnected_at = at;
    this.connection.last_close = { code, reason: trunc(reason, 120), wasClean: !!wasClean, at };
    this.record('websocket_close', { code, reason, wasClean });
  }

  noteError(readyState) {
    this.connection.ready_state = readyState ?? this.connection.ready_state;
    this.record('websocket_error', { ready_state: this.connection.ready_state });
  }

  noteReconnect(attempt, maxAttempts) {
    this.connection.reconnect_attempts = attempt;
    this.record('websocket_reconnect', { attempt, max_attempts: maxAttempts });
  }

  noteOutbox(length, action = 'queue') {
    this.connection.outbox_length = length;
    this.record(`outbox_${action}`, { outbox_length: length });
  }

  /** Mirror console.warn/error into the buffer (bounded + redacted). */
  patchConsole(consoleObj = console) {
    if (this._consolePatched) return;
    this._consolePatched = true;
    for (const level of ['warn', 'error']) {
      const original = consoleObj[level];
      if (typeof original !== 'function') continue;
      consoleObj[level] = (...args) => {
        this.record('console', {
          level,
          message: trunc(args.map((a) => {
            if (a instanceof Error) return `${a.name}: ${a.message}`;
            return typeof a === 'object' ? JSON.stringify(a) : String(a);
          }).join(' '), MAX_MESSAGE),
        });
        return original.apply(consoleObj, args);
      };
    }
  }

  /**
   * Snapshot for a feedback submission. Returns a plain, size-bounded object.
   */
  snapshot({ threadId = null, agentMode = null, llmModel = null } = {}) {
    this._prune();
    return {
      thread_id: threadId,
      captured_at: new Date(this._now()).toISOString(),
      url: trunc(typeof location !== 'undefined' ? location.href : null),
      user_agent: trunc(typeof navigator !== 'undefined' ? navigator.userAgent : null, 200),
      agent_mode: agentMode,
      llm_model: llmModel,
      connection: { ...this.connection },
      recent_events: this._events.slice(-this._maxEvents),
    };
  }
}

/** Shared instance — one page, one buffer. */
export const leoDiagnostics = new LeoDiagnostics();
