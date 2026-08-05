/**
 * Reports browser-side chat errors to the mothership.
 *
 * Error telemetry used to be backend-only: the FastAPI side reports Python
 * exceptions, but a client-side socket drop raises nothing server-side. Kody hit
 * "Lost connection" mid-run on 2026-07-24 and it produced no InstanceError row at
 * all — zero trace of a real, user-visible failure.
 *
 * Posts same-origin to /api/frontend-error (the browser is already authed to this
 * box), which forwards to the mothership with source="frontend".
 *
 * Guardrails, because this runs in a page that is already misbehaving:
 *  - dedupe by fingerprint per page load
 *  - hard cap on total reports per page load
 *  - every failure is swallowed; an error in the reporter must never cascade
 */

const ENDPOINT = '/api/frontend-error';
const MAX_REPORTS_PER_PAGE = 20;
const MAX_MESSAGE_CHARS = 2000;
const MAX_STACK_CHARS = 5000;

/** Cheap, stable, dependency-free hash — only needs to group like with like. */
function fingerprintOf(errorClass, message, agentMode) {
  const basis = `${errorClass}|${String(message || '').split('\n')[0].slice(0, 160)}|${agentMode}`;
  let h = 5381;
  for (let i = 0; i < basis.length; i++) {
    h = ((h << 5) + h + basis.charCodeAt(i)) | 0;
  }
  return `fe-${(h >>> 0).toString(16)}`;
}

export class ErrorReporter {
  constructor(appState = null) {
    this.appState = appState;
    this._seen = new Set();
    this._sent = 0;
    this._installed = false;
  }

  /** Called by index.js once AppState exists. */
  setAppState(appState) {
    this.appState = appState;
  }

  /**
   * Report one error. Never throws, never returns a rejected promise.
   * @param {string} errorClass e.g. 'FrontendError' | 'FrontendConnectionLost'
   */
  report(errorClass, message, stack = '', extra = {}) {
    try {
      if (this._sent >= MAX_REPORTS_PER_PAGE) return;

      const agentMode = extra.agent_mode ?? this._agentMode();
      const fingerprint = fingerprintOf(errorClass, message, agentMode);
      if (this._seen.has(fingerprint)) return;
      this._seen.add(fingerprint);
      this._sent += 1;

      const payload = {
        error_class: errorClass,
        error_message: String(message || '').slice(0, MAX_MESSAGE_CHARS),
        stack: String(stack || '').slice(0, MAX_STACK_CHARS),
        thread_id: extra.thread_id ?? this._threadId(),
        agent_mode: agentMode,
        model: extra.model ?? this._model(),
        fingerprint,
      };

      // Deliberately not awaited — reporting must never sit in the user's path.
      fetch(ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
        keepalive: true,   // survives a page teardown mid-report
      }).catch(() => {});
    } catch (_e) {
      // An error inside the error reporter is the one thing we must never surface.
    }
  }

  /** Install the global handlers. Idempotent. */
  install() {
    if (this._installed) return;
    this._installed = true;

    window.addEventListener('error', (event) => {
      const where = event.filename ? `${event.filename}:${event.lineno}:${event.colno}` : '';
      this.report(
        'FrontendError',
        `${event.message}${where ? ` (${where})` : ''}`,
        event.error && event.error.stack ? event.error.stack : '',
      );
    });

    window.addEventListener('unhandledrejection', (event) => {
      const reason = event.reason;
      const message = reason && reason.message ? reason.message : String(reason);
      this.report(
        'FrontendUnhandledRejection',
        message,
        reason && reason.stack ? reason.stack : '',
      );
    });
  }

  _threadId() {
    try {
      return this.appState?.getThreadId?.() || null;
    } catch (_e) {
      return null;
    }
  }

  _agentMode() {
    try {
      return this.appState?.getAgentConfig?.()?.name || null;
    } catch (_e) {
      return null;
    }
  }

  _model() {
    try {
      // The selected model lives in the DOM (the model <select>), not AppState.
      return document.querySelector('[data-llamabot="model-select"]')?.value
        || window.chatApp?.elements?.modelSelect?.value
        || null;
    } catch (_e) {
      return null;
    }
  }
}

/** Shared instance — the page only ever needs one. */
export const errorReporter = new ErrorReporter();
