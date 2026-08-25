/**
 * GitHubAuthModal - GitHub Device Flow OAuth modal
 *
 * Shows a modal with the device code and link to github.com/login/device.
 * Polls the backend until the user completes authorization — see
 * DeviceAuthPoller below for why that poll is not a plain setInterval.
 */

export const POLL_ENDPOINT = '/api/github/poll-auth';
export const DEFAULT_INTERVAL = 5;      // seconds; GitHub's own floor
export const MIN_WATCH_SECONDS = 120;   // keep watching this long even if told otherwise
export const MAX_TRANSIENT_ERRORS = 3;  // consecutive network/5xx failures before giving up

/**
 * Drives the "waiting for authorization" wait so the user never has to press
 * "check now". Three things a naive setInterval gets wrong:
 *
 *   - The user authorizes on github.com in ANOTHER tab, so this one is
 *     backgrounded and its timers are throttled to roughly once a minute. We
 *     re-check the moment the tab is looked at again (visibility/focus/online).
 *   - The successful poll installs the token on the host (gh auth login +
 *     docker cp), which can take tens of seconds. A timer firing underneath it
 *     would ask GitHub about an already-redeemed device code and paint an error
 *     over the success — so only one check is ever in flight.
 *   - A single transient 5xx must not end the wait.
 *
 * Timers, fetch and the clock are injectable so this is testable without
 * waiting on real time (see app/tests/js/github_device_autopoll.test.mjs).
 */
export class DeviceAuthPoller {
  constructor({
    deviceCode,
    interval = DEFAULT_INTERVAL,
    expiresIn = 900,
    fetchFn = (...args) => fetch(...args),
    timers = { setTimeout: (...a) => setTimeout(...a), clearTimeout: (...a) => clearTimeout(...a) },
    doc = typeof document !== 'undefined' ? document : null,
    win = typeof window !== 'undefined' ? window : null,
    now = () => Date.now(),
    onResult = () => {},
    onCheckingChange = () => {},
  } = {}) {
    this.deviceCode = deviceCode;
    this.intervalSeconds = Math.max(interval || DEFAULT_INTERVAL, DEFAULT_INTERVAL);
    this.expiresIn = expiresIn;
    this.fetchFn = fetchFn;
    this.timers = timers;
    this.doc = doc;
    this.win = win;
    this.now = now;
    this.onResult = onResult;
    this.onCheckingChange = onCheckingChange;

    this.stopped = false;
    this.inFlight = false;
    this.timer = null;
    this.deadline = null;
    this.lastCheckAt = null;
    this.transientErrors = 0;
    this.wakeEvents = [];
    this._onWake = () => { this.wake(); };
  }

  /** Begin the wait: hook the wake events and schedule the first check. */
  start() {
    if (this.stopped) return;
    this.lastCheckAt = this.now();
    this.deadline = this.now() + Math.max(this.expiresIn || 0, MIN_WATCH_SECONDS) * 1000;
    this._listen();
    this._schedule();
  }

  /**
   * The tab came back (or the network did). Check right away if GitHub's
   * interval floor has elapsed; otherwise leave the pending timer alone.
   */
  wake() {
    if (this.stopped || this.inFlight) return;
    const since = this.now() - (this.lastCheckAt ?? 0);
    if (since >= this.intervalSeconds * 1000) {
      this.timers.clearTimeout(this.timer);
      this.timer = null;
      this.check();
    }
  }

  /**
   * One poll. Safe to call from the timer, a wake, or the manual button — a
   * check already in flight simply wins.
   */
  async check() {
    if (this.stopped || this.inFlight) return null;
    this.inFlight = true;
    this.lastCheckAt = this.now();
    this.onCheckingChange(true);

    let status = null;
    try {
      const resp = await this.fetchFn(POLL_ENDPOINT, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_code: this.deviceCode }),
      });

      if (!resp.ok) {
        status = this._transient('Server error while checking GitHub');
      } else {
        const data = await resp.json();
        this.transientErrors = 0;
        status = data.status || 'error';
        this._handle(status, data);
      }
    } catch (e) {
      status = this._transient(e?.message || 'Network error while checking GitHub');
    } finally {
      this.inFlight = false;
      this.onCheckingChange(false);
      if (!this.stopped) this._schedule();
    }
    return status;
  }

  /** Stop polling and unhook the wake listeners. Idempotent. */
  stop() {
    this.stopped = true;
    this.timers.clearTimeout(this.timer);
    this.timer = null;
    this._unlisten();
  }

  _handle(status, data) {
    switch (status) {
      case 'pending':
        this.onResult('pending', data);
        break;
      case 'slow_down':
        // GitHub asks for more room between polls; take it and keep waiting.
        this.intervalSeconds = Math.max(data.interval || this.intervalSeconds + 5, this.intervalSeconds + 5);
        this.onResult('slow_down', data);
        break;
      case 'success':
      case 'expired':
      case 'denied':
        this.stop();
        this.onResult(status, data);
        break;
      default:
        this.stop();
        this.onResult('error', data);
    }
  }

  /** Ride out the odd network blip; give up only if they keep coming. */
  _transient(message) {
    this.transientErrors += 1;
    if (this.transientErrors > MAX_TRANSIENT_ERRORS) {
      this.stop();
      this.onResult('error', { message });
      return 'error';
    }
    console.warn('GitHub poll error:', message);
    return 'retry';
  }

  _schedule() {
    if (this.stopped) return;
    if (this.deadline !== null && this.now() >= this.deadline) {
      this.stop();
      this.onResult('expired', { message: 'Authorization code expired. Please try again.' });
      return;
    }
    this.timers.clearTimeout(this.timer);
    this.timer = this.timers.setTimeout(() => { this.timer = null; return this.check(); }, this.intervalSeconds * 1000);
  }

  _listen() {
    this._unlisten();
    if (this.doc?.addEventListener) {
      this.doc.addEventListener('visibilitychange', this._onWake);
      this.wakeEvents.push([this.doc, 'visibilitychange']);
    }
    if (this.win?.addEventListener) {
      this.win.addEventListener('focus', this._onWake);
      this.win.addEventListener('online', this._onWake);
      this.wakeEvents.push([this.win, 'focus'], [this.win, 'online']);
    }
  }

  _unlisten() {
    this.wakeEvents.forEach(([target, name]) => target.removeEventListener(name, this._onWake));
    this.wakeEvents = [];
  }
}

export class GitHubAuthModal {
  constructor(options = {}) {
    this.modal = null;
    this.poller = null;
    this.deviceCode = null;
    this.aborted = false;
    // Injection seam for tests; the browser gets the real globals.
    this.pollerOptions = options.pollerOptions || {};
  }

  /**
   * Start the GitHub auth flow - creates modal & begins polling
   */
  async start() {
    this.aborted = false;

    // First check if already authenticated
    try {
      const statusResp = await fetch('/api/github/status', { credentials: 'same-origin' });
      if (statusResp.ok) {
        const statusData = await statusResp.json();
        if (statusData.authenticated) {
          this.showAlreadyAuthenticated(statusData.output);
          return;
        }
      }
    } catch (e) {
      // Continue with auth flow
    }

    // Request device code from backend
    try {
      const resp = await fetch('/api/github/device-code', {
        method: 'POST',
        credentials: 'same-origin',
      });

      if (!resp.ok) {
        const err = await resp.json();
        this.showError(err.detail || 'Failed to start GitHub auth');
        return;
      }

      const data = await resp.json();
      this.deviceCode = data.device_code;
      this.showModal(data.user_code, data.verification_uri, data.interval);
      this.startPolling(data.device_code, data.interval, data.expires_in);
    } catch (e) {
      this.showError('Failed to connect to server: ' + e.message);
    }
  }

  showAlreadyAuthenticated(output) {
    this.createModal(`
      <div class="gh-auth-modal-header">
        <svg class="gh-octocat" viewBox="0 0 16 16" width="32" height="32">
          <path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
        </svg>
        <span>GitHub Connected</span>
      </div>
      <div class="gh-auth-modal-body">
        <div class="gh-auth-success">
          <i class="fa-solid fa-circle-check"></i>
          <p>Already authenticated with GitHub!</p>
        </div>
        <div class="gh-auth-status-output">${this.escapeHtml(output)}</div>
      </div>
      <div class="gh-auth-modal-actions">
        <button class="gh-auth-btn-close">Close</button>
      </div>
    `);

    this.modal.querySelector('.gh-auth-btn-close').onclick = () => this.close();
  }

  showModal(userCode, verificationUri, interval) {
    this.createModal(`
      <div class="gh-auth-modal-header">
        <svg class="gh-octocat" viewBox="0 0 16 16" width="32" height="32">
          <path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
        </svg>
        <span>Connect to GitHub</span>
      </div>
      <div class="gh-auth-modal-body">
        <div class="gh-auth-steps">
          <div class="gh-auth-step">
            <span class="gh-auth-step-num">1</span>
            <span>Open GitHub and enter this code:</span>
          </div>
          <div class="gh-auth-code-box">
            <code class="gh-auth-code">${userCode}</code>
            <button class="gh-auth-copy-btn" title="Copy code">
              <i class="fa-regular fa-copy"></i>
            </button>
          </div>
          <div class="gh-auth-step">
            <span class="gh-auth-step-num">2</span>
            <a href="${verificationUri}" target="_blank" rel="noopener" class="gh-auth-link">
              Open github.com/login/device
              <i class="fa-solid fa-arrow-up-right-from-square"></i>
            </a>
          </div>
        </div>
        <div class="gh-auth-waiting">
          <i class="fa-solid fa-spinner fa-spin"></i>
          <span class="gh-auth-waiting-text">Waiting for authorization — this checks itself, no need to refresh.</span>
          <button class="gh-auth-check-btn" title="Check now">
            <i class="fa-solid fa-rotate"></i>
          </button>
        </div>
      </div>
      <div class="gh-auth-modal-actions">
        <button class="gh-auth-btn-cancel">Cancel</button>
      </div>
    `);

    // Copy button
    this.modal.querySelector('.gh-auth-copy-btn').onclick = async () => {
      try {
        await navigator.clipboard.writeText(userCode);
        const btn = this.modal.querySelector('.gh-auth-copy-btn');
        btn.innerHTML = '<i class="fa-solid fa-check"></i>';
        setTimeout(() => { btn.innerHTML = '<i class="fa-regular fa-copy"></i>'; }, 2000);
      } catch (e) {
        // Select the code text as fallback
        const codeEl = this.modal.querySelector('.gh-auth-code');
        const range = document.createRange();
        range.selectNodeContents(codeEl);
        window.getSelection().removeAllRanges();
        window.getSelection().addRange(range);
      }
    };

    // Check now button — the flow polls itself; this is just an impatience valve.
    this.modal.querySelector('.gh-auth-check-btn').onclick = () => {
      this.poller?.wake();
    };

    // Cancel button
    this.modal.querySelector('.gh-auth-btn-cancel').onclick = () => this.close();

    // Close on backdrop click
    this.modal.onclick = (e) => {
      if (e.target === this.modal) this.close();
    };
  }

  showSuccess(message) {
    const body = this.modal?.querySelector('.gh-auth-modal-body');
    if (!body) return;

    body.innerHTML = `
      <div class="gh-auth-success">
        <i class="fa-solid fa-circle-check"></i>
        <p>${this.escapeHtml(message)}</p>
      </div>
    `;

    const actions = this.modal.querySelector('.gh-auth-modal-actions');
    actions.innerHTML = '<button class="gh-auth-btn-close">Close</button>';
    actions.querySelector('.gh-auth-btn-close').onclick = () => this.close();
  }

  showError(message) {
    if (!this.modal) {
      this.createModal(`
        <div class="gh-auth-modal-header">
          <svg class="gh-octocat" viewBox="0 0 16 16" width="32" height="32">
            <path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z"/>
          </svg>
          <span>GitHub Auth Error</span>
        </div>
        <div class="gh-auth-modal-body">
          <div class="gh-auth-error">
            <i class="fa-solid fa-circle-xmark"></i>
            <p>${this.escapeHtml(message)}</p>
          </div>
        </div>
        <div class="gh-auth-modal-actions">
          <button class="gh-auth-btn-close">Close</button>
        </div>
      `);
      this.modal.querySelector('.gh-auth-btn-close').onclick = () => this.close();
      return;
    }

    const body = this.modal.querySelector('.gh-auth-modal-body');
    if (body) {
      body.innerHTML = `
        <div class="gh-auth-error">
          <i class="fa-solid fa-circle-xmark"></i>
          <p>${this.escapeHtml(message)}</p>
        </div>
      `;
    }

    const actions = this.modal.querySelector('.gh-auth-modal-actions');
    if (actions) {
      actions.innerHTML = '<button class="gh-auth-btn-close">Close</button>';
      actions.querySelector('.gh-auth-btn-close').onclick = () => this.close();
    }
  }

  createModal(innerHTML) {
    // Remove any existing modal
    this.close();

    this.modal = document.createElement('div');
    this.modal.className = 'gh-auth-modal';
    this.modal.innerHTML = `<div class="gh-auth-modal-content">${innerHTML}</div>`;
    document.body.appendChild(this.modal);
  }

  /**
   * Wait for the user to finish on github.com. DeviceAuthPoller keeps checking
   * on its own — including the instant the user switches back to this tab,
   * which is when a plain interval would still be throttled — so the "check
   * now" button is optional rather than the way the flow completes.
   */
  startPolling(deviceCode, interval, expiresIn) {
    this.stopPolling();
    this.deviceCode = deviceCode;
    this.poller = new DeviceAuthPoller({
      deviceCode,
      interval,
      expiresIn,
      onCheckingChange: (checking) => this.setChecking(checking),
      onResult: (status, data = {}) => {
        switch (status) {
          case 'success':
            this.showSuccess(data.message || 'GitHub connected!');
            break;
          case 'expired':
            this.showError(data.message || 'Authorization code expired. Please try again.');
            break;
          case 'denied':
            this.showError('Authorization was denied.');
            break;
          case 'error':
            this.showError(data.message || 'Unexpected error');
            break;
          default:
            break;  // pending / slow_down — keep waiting
        }
      },
      ...this.pollerOptions,
    });
    this.poller.start();
  }

  /** Spin the check button while a poll is in flight. */
  setChecking(checking) {
    const btn = this.modal?.querySelector('.gh-auth-check-btn');
    if (!btn) return;
    btn.innerHTML = checking
      ? '<i class="fa-solid fa-rotate fa-spin"></i>'
      : '<i class="fa-solid fa-rotate"></i>';
    btn.disabled = checking;
  }

  stopPolling() {
    if (this.poller) {
      this.poller.stop();
      this.poller = null;
    }
  }

  close() {
    this.aborted = true;
    this.stopPolling();
    if (this.modal) {
      this.modal.remove();
      this.modal = null;
    }
  }

  escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }
}
