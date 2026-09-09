/**
 * ErrorAttach — surfaces JavaScript errors that happen inside the Rails preview
 * iframe, and lets the user attach the ones they care about to their message.
 *
 * The chat page and the app preview are different origins, so the browser will
 * not let us read the iframe's errors directly. We do not need it to: the Rails
 * app pushes them to us (llamapress/console_capture.js -> postMessage
 * {type:'js-error'}), and this class is the receiving half.
 *
 * Why a push channel and not the existing `get-console-logs` request: that one
 * DRAINS the iframe's buffer on read, so a passive error tray and the
 * capture-logs button would steal logs from each other. This keeps its own list.
 *
 * UI: one quiet line above the composer — "1 error detected" plus a
 * default-checked "Show Leo" box. Checked (the default) means the errors ride
 * along with the next message and are consumed by it; unchecking holds them
 * back. Deliberately understated: this sits under the composer the whole time
 * something is broken, so it must read as a footnote, not an alarm.
 *
 * The error text itself lives behind a "Read more" popup rather than an inline
 * list, so the row costs one line no matter how much has gone wrong. The popup
 * leads with plain language — a non-developer should be able to tell what broke
 * without reading a stack trace.
 */

import { getRailsUrl } from '../config.js';
import { isExtensionOnly } from './extensionErrorFilter.js';

/** Keep the tray useful, not a log file. Oldest fall off the end. */
const MAX_ERRORS = 25;

export class ErrorAttach {
  constructor({ getAllowedOrigin = getRailsUrl } = {}) {
    this.getAllowedOrigin = getAllowedOrigin;
    this.banner = null;
    this.messageInput = null;
    this.errors = [];       // {id, kind, message, stack, path, timestamp, count}
    this.showLeo = true;    // send them with the next message (the default)
    this.dismissed = false; // user closed the banner; a NEW error reopens it
    this.modal = null;      // the "Read more" popup, while it is open
    this._onMessage = this._onMessage.bind(this);
  }

  /**
   * @param {HTMLElement} banner - container for the banner UI (above the input)
   * @param {HTMLTextAreaElement} messageInput - chat input (kept for focus)
   */
  init(banner, messageInput) {
    this.banner = banner;
    this.messageInput = messageInput;
    window.addEventListener('message', this._onMessage);
    this.render();
  }

  destroy() {
    window.removeEventListener('message', this._onMessage);
  }

  // ==========================================================================
  // Receiving
  // ==========================================================================

  _onMessage(event) {
    const data = event && event.data;
    if (!data || data.source !== 'llamapress' || data.type !== 'js-error') return;

    // The frame is untrusted input like every other postMessage sender: only the
    // configured Rails origin may put entries in a tray the user will paste into
    // a prompt. A second framed page cannot inject fake "errors" here.
    let allowed;
    try { allowed = this.getAllowedOrigin(); } catch { allowed = null; }
    if (!allowed || event.origin !== allowed) return;

    this.record(data.error);
  }

  /**
   * Add one error. Repeats of the same message on the same page collapse into a
   * count instead of filling the tray — a render loop can fire the same
   * TypeError hundreds of times, and 200 identical rows help nobody.
   */
  record(raw) {
    if (!raw || !raw.message) return;
    // A browser extension running inside the preview throws on the app's own
    // window, so its errors reach us from the right origin looking like the
    // app's. They are not, and the tray feeds the user's prompt.
    if (isExtensionOnly(raw)) return;

    const entry = {
      id: String(raw.id || `${Date.now()}-${this.errors.length}`),
      kind: String(raw.kind || 'error'),
      message: String(raw.message).slice(0, 2000),
      stack: raw.stack ? String(raw.stack).slice(0, 4000) : null,
      path: String(raw.path || ''),
      timestamp: Number(raw.timestamp) || Date.now(),
      // Server errors arrive with a total already on them: the Rails feed
      // collapses a render loop into a count rather than 200 entries, so this is
      // the only place that number exists. Pushed JS errors carry no count and
      // are tallied here instead.
      count: Math.max(1, Number(raw.count) || 1),
      counted: Number(raw.count) > 0
    };

    const dupe = this.errors.find((e) => e.message === entry.message && e.path === entry.path);
    if (dupe) {
      // A source that counts for itself re-reports its running total, so taking
      // the larger is right and incrementing would double-count it.
      dupe.count = entry.counted ? Math.max(dupe.count, entry.count) : dupe.count + 1;
      dupe.timestamp = entry.timestamp;
    } else {
      this.errors.push(entry);
      if (this.errors.length > MAX_ERRORS) this.errors.shift();
      this.dismissed = false;  // a genuinely new error is worth re-showing
    }
    this.render();
  }

  // ==========================================================================
  // Actions
  // ==========================================================================

  /** The "Show Leo" box. Off means the errors stay put and nothing is sent. */
  toggleShowLeo() {
    this.showLeo = !this.showLeo;
    this.render();
  }

  /**
   * The × — "not interested". It hides the row AND stops the errors shipping, so
   * dismissing can never leave something silently attached to the next message.
   */
  dismiss() {
    this.dismissed = true;
    this.closeDetails();
    this.render();
  }

  /** Throw the tray away. */
  clearErrors() {
    this.errors = [];
    this.closeDetails();
    this.render();
  }

  /**
   * The block appended to the outgoing message. Kept here (not in index.js) so
   * the wire format is testable without booting the whole chat app.
   */
  buildMessageBlock() {
    if (!this.sending()) return '';

    // Two tags, because they are two different jobs. A Ruby backtrace described
    // as "a JavaScript error from their app preview" would send Leo looking in
    // the wrong half of the app — and a 500 is fixed in a controller or a view,
    // not in the page's script.
    const blocks = [];

    const js = this.errors.filter((e) => e.kind !== 'rails');
    if (js.length > 0) {
      const plural = js.length > 1 ? 's' : '';
      blocks.push(`<PAGE_JS_ERRORS>\nThe user attached the following JavaScript error${plural} `
        + `from their app preview:\n\n${formatErrors(js)}\n</PAGE_JS_ERRORS>`);
    }

    const rails = this.errors.filter((e) => e.kind === 'rails');
    if (rails.length > 0) {
      const plural = rails.length > 1 ? 's' : '';
      blocks.push(`<RAILS_SERVER_ERRORS>\nThe user attached the following error${plural} `
        + `their Rails app raised while they were using it:\n\n${formatErrors(rails)}\n`
        + `</RAILS_SERVER_ERRORS>`);
    }

    return blocks.join('\n\n');
  }

  /** True when the current errors would ride along with a send. */
  sending() {
    return this.showLeo && !this.dismissed && this.errors.length > 0;
  }

  /**
   * Called by index.js after a send. The errors went out with the message, so
   * they are consumed; if they did NOT go out (box unchecked, row dismissed)
   * they are left exactly as they were, so the user can change their mind.
   */
  clear() {
    if (!this.sending()) return;
    this.errors = [];
    this.closeDetails();   // it is describing errors that are now gone
    this.render();
  }

  // ==========================================================================
  // Rendering
  // ==========================================================================

  render() {
    if (!this.banner) return;

    if (this.errors.length === 0 || this.dismissed) {
      this.banner.classList.add('hidden');
      this.banner.innerHTML = '';
      return;
    }

    // Distinct problems, not total occurrences. One broken page that raises the
    // same error on every render is one thing to fix, and Rails counts those in
    // the dozens — "50 errors detected" would be alarming and wrong. How many
    // times each one fired is in the popup, on the entry it belongs to, which is
    // also the only place it means anything.
    this.banner.classList.remove('hidden');
    this.banner.innerHTML = `
      <span class="js-error-dot"></span>
      <span class="js-error-count">${this._countLabel()}</span>
      <button type="button" class="js-error-more" data-act="details">Read more</button>
      <label class="js-error-show">
        <input type="checkbox" data-act="show" ${this.showLeo ? 'checked' : ''} />
        <span>Show Leo</span>
      </label>
      <button type="button" class="js-error-x" data-act="dismiss"
              title="Dismiss" aria-label="Dismiss">&times;</button>
    `;

    this.banner.querySelectorAll('[data-act]').forEach((el) => {
      el.addEventListener('click', (ev) => {
        const act = el.dataset.act;
        if (act === 'dismiss') { ev.preventDefault(); this.dismiss(); }
        else if (act === 'details') { ev.preventDefault(); this.openDetails(); }
        else if (act === 'show') this.toggleShowLeo();
      });
    });
  }

  // ==========================================================================
  // The "Read more" popup
  // ==========================================================================

  /**
   * Open the details popup. Built on demand and thrown away on close — it is
   * describing a list that changes, so there is nothing worth keeping around.
   */
  /**
   * "1 Rails error, 2 JavaScript errors detected".
   *
   * Naming the kind is the whole point of the line. The two mean different
   * things to whoever is looking: a JavaScript error means the page loaded and
   * something on it misbehaved; a Rails error means the request never made it
   * out of the server — and those happen on screens that look completely fine,
   * which is exactly when nobody would think to look.
   */
  _countLabel() {
    const rails = this.errors.filter((e) => e.kind === 'rails').length;
    const js = this.errors.length - rails;
    const part = (n, name) => `${n} ${name} error${n === 1 ? '' : 's'}`;

    const parts = [];
    if (rails) parts.push(part(rails, 'Rails'));
    if (js) parts.push(part(js, 'JavaScript'));
    return `${parts.join(', ')} detected`;
  }

  openDetails() {
    if (this.modal) return;              // already open; don't stack a second one
    if (this.errors.length === 0) return;

    const overlay = document.createElement('div');
    overlay.className = 'js-error-modal-overlay';
    overlay.innerHTML = this._detailsHtml();

    // Backdrop click closes, a click inside must not.
    overlay.addEventListener('click', (ev) => {
      if (ev.target === overlay || (ev.target.dataset && ev.target.dataset.act === 'close')) {
        this.closeDetails();
      }
    });
    this._onModalKey = (ev) => { if (ev.key === 'Escape') this.closeDetails(); };
    document.addEventListener('keydown', this._onModalKey);

    document.body.appendChild(overlay);
    this.modal = overlay;
  }

  closeDetails() {
    if (this._onModalKey) {
      document.removeEventListener('keydown', this._onModalKey);
      this._onModalKey = null;
    }
    if (!this.modal) return;
    this.modal.remove();
    this.modal = null;
  }

  _detailsHtml() {
    const plural = this.errors.length === 1 ? '' : 's';
    // Say what happens next in the user's terms, and make it depend on the
    // actual state of the box — telling someone Leo will see this when the box
    // is off would be a lie.
    const next = this.showLeo
      ? `Leo will see the details below with your next message, so you can just ask him to fix it.`
      : `These aren't being sent right now — tick <strong>Show Leo</strong> on the notice to include them with your next message.`;

    const items = this.errors.map((e) => `
      <li class="js-error-item${e.kind === 'rails' ? ' js-error-item--rails' : ''}">
        <span class="js-error-kind">${kindLabel(e)}</span>
        <div class="js-error-item-plain">${escapeHtml(friendlySummary(e))}</div>
        <div class="js-error-item-tech">${escapeHtml(e.message)}</div>
        <div class="js-error-item-meta">
          ${e.path ? `on <code>${escapeHtml(e.path)}</code>` : ''}
          ${e.count > 1 ? `&middot; happened ${e.count} times` : ''}
        </div>
        ${e.stack ? `<details class="js-error-item-stack">
          <summary>Technical details</summary>
          <pre>${escapeHtml(e.stack)}</pre>
        </details>` : ''}
      </li>`).join('');

    return `
      <div class="js-error-modal-card" role="dialog" aria-modal="true" aria-label="Errors on this page">
        <div class="js-error-modal-head">
          <span class="js-error-modal-badge"><i class="fa-solid fa-triangle-exclamation"></i></span>
          <h2>Something on this page isn't working</h2>
          <button type="button" class="js-error-modal-x" data-act="close" aria-label="Close">&times;</button>
        </div>
        <p class="js-error-modal-sub">
          Your app ran into ${this.errors.length} problem${plural} while you were using it. ${next}
        </p>
        <ul class="js-error-modal-list">${items}</ul>
        <div class="js-error-modal-actions">
          <button type="button" class="js-error-modal-btn" data-act="close">Got it</button>
        </div>
      </div>`;
  }
}

/**
 * One plain sentence for what went wrong. A non-developer should be able to read
 * the popup and know roughly what happened without parsing "TypeError".
 * The technical message is shown alongside, never replaced.
 */
export function friendlySummary(e) {
  const message = String((e && e.message) || '');
  const kind = (e && e.kind) || '';

  // Server errors first: a Ruby NoMethodError would otherwise be described in
  // the language of the browser, which is both wrong and unhelpful.
  if (kind === 'rails') return serverSummary(message);

  if (/NetworkError|Failed to fetch|fetch failed|ERR_|status (4|5)\d\d/i.test(message)) {
    return "A request to the server didn't go through.";
  }
  if (/ReferenceError/.test(message)) {
    return "The page tried to use something that doesn't exist.";
  }
  if (/TypeError/.test(message)) {
    return "The page used a value in a way it can't be used.";
  }
  if (/SyntaxError/.test(message)) {
    return "There's a mistake in the page's code.";
  }
  if (/RangeError/.test(message)) {
    return 'The page went past a limit it was allowed.';
  }
  if (kind === 'unhandled-rejection') {
    return 'A background task failed and nothing caught it.';
  }
  return 'The page hit an unexpected error.';
}

/**
 * Which half of the stack this came from, as a word the user can act on.
 *
 * Derived from a fixed two-way mapping rather than echoing `e.kind`, so an
 * app-controlled value can never reach the markup.
 */
export function kindLabel(e) {
  return (e && e.kind) === 'rails' ? 'Rails' : 'JavaScript';
}

/** The same job as friendlySummary, for the exceptions Rails raises. */
function serverSummary(message) {
  if (/PendingMigration/.test(message)) {
    return 'The database is missing a change the app expects.';
  }
  if (/RecordNotFound/.test(message)) {
    return "The app asked for something in the database that doesn't exist.";
  }
  if (/StatementInvalid|PG::|ActiveRecord::/.test(message)) {
    return "The database couldn't do what the app asked.";
  }
  if (/MissingTemplate|ActionView::/.test(message)) {
    return 'The server hit an error while building this page.';
  }
  if (/RoutingError|ActionController::/.test(message)) {
    return "The server didn't know how to handle that request.";
  }
  return 'The server hit an error handling that request.';
}

/** One line per error, shared by both blocks sent to Leo. */
function formatErrors(errors) {
  return errors.map((e) => {
    const times = e.count > 1 ? ` (x${e.count})` : '';
    const where = e.path ? ` on ${e.path}` : '';
    const stack = e.stack ? `\n${e.stack}` : '';
    return `[${e.kind}]${where}${times}: ${e.message}${stack}`;
  }).join('\n\n');
}

function truncate(s, n) {
  const v = String(s);
  return v.length > n ? v.slice(0, n - 1) + '…' : v;
}

/** Error text is app-controlled and goes into innerHTML — escape it. */
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
