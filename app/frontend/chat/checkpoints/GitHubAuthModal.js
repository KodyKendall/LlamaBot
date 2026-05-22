/**
 * GitHubAuthModal - GitHub Device Flow OAuth modal
 *
 * Shows a modal with the device code and link to github.com/login/device.
 * Polls the backend until the user completes authorization.
 */

export class GitHubAuthModal {
  constructor() {
    this.modal = null;
    this.pollInterval = null;
    this.deviceCode = null;
    this.aborted = false;
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
      this.startPolling(data.device_code, data.interval);
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
          <span>Waiting for authorization...</span>
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

    // Check now button - manual poll trigger
    this.modal.querySelector('.gh-auth-check-btn').onclick = () => {
      this.pollOnce(this.deviceCode);
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

  startPolling(deviceCode, interval) {
    const pollMs = (interval || 5) * 1000;

    this.pollInterval = setInterval(async () => {
      if (this.aborted) {
        this.stopPolling();
        return;
      }

      try {
        const resp = await fetch('/api/github/poll-auth', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ device_code: deviceCode }),
        });

        if (!resp.ok) {
          this.showError('Server error while polling');
          this.stopPolling();
          return;
        }

        const data = await resp.json();

        switch (data.status) {
          case 'success':
            this.stopPolling();
            this.showSuccess(data.message || 'GitHub connected!');
            break;
          case 'pending':
            // Keep polling
            break;
          case 'slow_down':
            // GitHub wants us to slow down - restart with longer interval
            this.stopPolling();
            this.startPolling(deviceCode, data.interval || 10);
            break;
          case 'expired':
            this.stopPolling();
            this.showError('Authorization code expired. Please try again.');
            break;
          case 'denied':
            this.stopPolling();
            this.showError('Authorization was denied.');
            break;
          default:
            this.stopPolling();
            this.showError(data.message || 'Unexpected error');
        }
      } catch (e) {
        // Network error - keep trying
        console.warn('GitHub poll error:', e);
      }
    }, pollMs);
  }

  async pollOnce(deviceCode) {
    const checkBtn = this.modal?.querySelector('.gh-auth-check-btn');
    if (checkBtn) {
      checkBtn.innerHTML = '<i class="fa-solid fa-rotate fa-spin"></i>';
      checkBtn.disabled = true;
    }

    try {
      const resp = await fetch('/api/github/poll-auth', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device_code: deviceCode }),
      });

      if (!resp.ok) {
        this.showError('Server error while checking');
        this.stopPolling();
        return;
      }

      const data = await resp.json();

      if (data.status === 'success') {
        this.stopPolling();
        this.showSuccess(data.message || 'GitHub connected!');
      } else if (data.status === 'pending') {
        // Still waiting - restore button
        if (checkBtn) {
          checkBtn.innerHTML = '<i class="fa-solid fa-rotate"></i>';
          checkBtn.disabled = false;
        }
      } else if (data.status === 'expired') {
        this.stopPolling();
        this.showError('Authorization code expired. Please try again.');
      } else if (data.status === 'denied') {
        this.stopPolling();
        this.showError('Authorization was denied.');
      } else {
        this.stopPolling();
        this.showError(data.message || 'Unexpected error');
      }
    } catch (e) {
      if (checkBtn) {
        checkBtn.innerHTML = '<i class="fa-solid fa-rotate"></i>';
        checkBtn.disabled = false;
      }
    }
  }

  stopPolling() {
    if (this.pollInterval) {
      clearInterval(this.pollInterval);
      this.pollInterval = null;
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
