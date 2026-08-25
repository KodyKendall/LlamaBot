/**
 * RailsErrorPoll — puts the Rails app's crashes in the same tray as the
 * preview's JavaScript errors.
 *
 * The user presses a button in their app, it 500s, and until now the only sign
 * was a Rails error page inside the preview iframe — with no way to tell whether
 * Leo could see it. This closes that: the crash shows up in the notice above the
 * composer, next to the JS errors, and rides along with the next message.
 *
 * Why polling, when JavaScript errors are pushed: the Rails app has no handle on
 * the chat page and cannot postMessage anything. It keeps its last 25 crashes in
 * memory instead, behind `GET /llama_bot/errors?since=<seq>`, which LlamaBot
 * proxies at `/api/rails-errors` (the gem sets no CORS headers, so the browser
 * cannot read it directly). We read that with a cursor.
 *
 * Two things make the cursor worth care rather than a `while (true)`:
 *
 * 1. **Arming.** The first poll omits `since`, which the feed treats as a probe:
 *    tell me where you are, give me nothing. Crashes that predate the page are
 *    not something to greet the user with.
 * 2. **Rewinds.** The feed lives in Rails' memory, so a restart resets it to
 *    zero. A cursor left at 10 would ask for "everything after 10" forever and
 *    the tray would silently go deaf for the rest of the session.
 *
 * And one rule about whose problem an error is: while an agent turn is running,
 * anything that breaks is Leo's to fix — RailsErrorWatchMiddleware is already
 * polling the same feed and repairing mid-turn, deliberately without the user
 * seeing the broken page. Surfacing it here at the same moment would hand them a
 * problem that is already being handled. We advance past those and stay quiet.
 */

const DEFAULT_INTERVAL_MS = 4000;

// A box whose gem predates the feed answers "unavailable" forever. After a few
// of those, drop to a slow heartbeat — it still recovers on its own if the box
// updates mid-session, but it stops being a request every four seconds.
const QUIET_INTERVAL_MS = 60000;
const MISSES_BEFORE_QUIET = 3;

export class RailsErrorPoll {
  constructor({
    getToken,
    onErrors,
    isAgentRunning = () => false,
    isVisible = () => (typeof document === 'undefined'
      || document.visibilityState !== 'hidden'),
    fetchImpl = (...args) => fetch(...args),
    endpoint = '/api/rails-errors',
    intervalMs = DEFAULT_INTERVAL_MS,
    quietIntervalMs = QUIET_INTERVAL_MS,
  } = {}) {
    this.getToken = getToken;
    this.onErrors = onErrors;
    this.isAgentRunning = isAgentRunning;
    this.isVisible = isVisible;
    this.fetchImpl = fetchImpl;
    this.endpoint = endpoint;

    this.fastIntervalMs = intervalMs;
    this.quietIntervalMs = quietIntervalMs;
    this.intervalMs = intervalMs;

    this.cursor = null;   // null until the first answer arms us
    this.misses = 0;
    this._inFlight = false;
    this._timer = null;
  }

  // ==========================================================================
  // Lifecycle
  // ==========================================================================

  start() {
    if (this._timer) return;
    this.tick();
    this._schedule();
  }

  stop() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = null;
  }

  /**
   * Poll now rather than waiting for the next interval. Called when the preview
   * finishes loading a page: if that navigation was the 500, the crash is
   * already in the feed and the user should not wait four seconds to be told.
   */
  nudge() {
    this.tick();
  }

  _schedule() {
    if (this._timer) clearTimeout(this._timer);
    this._timer = setTimeout(() => {
      this.tick().finally(() => this._schedule());
    }, this.intervalMs);
  }

  // ==========================================================================
  // One poll
  // ==========================================================================

  /** Never rejects: a crash reporter that crashes the page helps nobody. */
  async tick() {
    if (this._inFlight) return;          // a slow answer must not stack requests
    if (!this.isVisible()) return;       // nobody is looking at the tray

    this._inFlight = true;
    try {
      await this._poll();
    } catch (e) {
      // Offline, CORS, a proxy hiccup. Hold the cursor and try again; losing our
      // place would be worse than missing one round.
      this._miss();
    } finally {
      this._inFlight = false;
    }
  }

  async _poll() {
    const token = await this.getToken();
    if (!token) {
      // Signed into LlamaBot but not into the Rails app. Nothing to read, and
      // no point telling the user about it.
      return;
    }

    const url = this.cursor === null
      ? this.endpoint
      : `${this.endpoint}?since=${encodeURIComponent(this.cursor)}`;

    const response = await this.fetchImpl(url, {
      headers: { 'X-Rails-Api-Token': token, Accept: 'application/json' },
    });
    if (!response || !response.ok) {
      this._miss();
      return;
    }

    const body = await response.json();
    if (!body || body.available !== true || typeof body.seq !== 'number') {
      this._miss();
      return;
    }

    this._hit();

    const previous = this.cursor;
    this.cursor = body.seq;

    if (previous === null) {
      // The arming probe. We now know where the log stands; that is all we came
      // for.
      return;
    }

    if (body.seq < previous) {
      // Rails restarted. Its ring is a fresh one whose entries we never asked
      // for, so re-arm at the new position and take nothing.
      return;
    }

    const errors = Array.isArray(body.errors) ? body.errors : [];
    if (errors.length === 0) return;

    // Mid-turn crashes belong to Leo's auto-recovery. The cursor has already
    // moved past them, so they do not resurface when the turn ends.
    if (this.isAgentRunning()) return;

    this.onErrors(errors);
  }

  _miss() {
    this.misses += 1;
    if (this.misses >= MISSES_BEFORE_QUIET) this.intervalMs = this.quietIntervalMs;
  }

  _hit() {
    this.misses = 0;
    this.intervalMs = this.fastIntervalMs;
  }
}
