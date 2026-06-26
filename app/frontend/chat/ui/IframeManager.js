/**
 * Iframe Manager
 *
 * Manages TWO separate iframe concepts:
 * 1. STREAMING PREVIEW (contentFrame - commented out in HTML)
 *    - Used for real-time HTML streaming preview
 *    - Shows agent-generated HTML as it's being built token-by-token
 *    - Has overlay animations during generation
 *
 * 2. RAILS APP PREVIEW (liveSiteFrame)
 *    - Shows the actual running Rails application
 *    - Refreshed after changes to show updated app
 *    - Requires Rails debug info to maintain state
 */

import { getRailsUrl, getVSCodeUrl, getTicketsUrl, getFeedbackUrl, DEFAULT_CONFIG } from '../config.js';

export class IframeManager {
  constructor(container = null) {
    this.container = container || document;

    // STREAMING PREVIEW iframe (for HTML generation preview)
    this.contentFrame = this.querySelector('[data-llamabot="content-frame"]');

    // RAILS APP PREVIEW iframe (for live Rails app)
    this.liveSiteFrame = this.querySelector('[data-llamabot="live-site-frame"]');

    // VS CODE iframe
    this.vsCodeFrame = this.querySelector('[data-llamabot="vscode-frame"]');

    // TICKETS iframe
    this.ticketsFrame = this.querySelector('[data-llamabot="tickets-frame"]');

    // FEEDBACK iframe
    this.feedbackFrame = this.querySelector('[data-llamabot="feedback-frame"]');

    // URL input element
    this.urlInput = this.querySelector('[data-llamabot="url-input"]');

    // URL dropdown element
    this.urlDropdown = this.querySelector('[data-llamabot="url-dropdown"]');

    // Cached routes
    this.cachedRoutes = null;

    this.overlayElement = null;

    // Navigation history stack for back button (since we can't access cross-origin iframe history)
    this.navigationHistory = [];

    // Track current path for reliable refresh (fallback when iframe query fails)
    this.currentPath = '/';

    // Initialize iframe URLs
    this.initIframeSources();

    // Listen for navigation messages from the Rails iframe
    this.initNavigationListener();
  }

  /**
   * Initialize listener for navigation messages from the Rails iframe
   * This allows us to track navigation that happens inside the iframe (link clicks, etc.)
   */
  initNavigationListener() {
    window.addEventListener('message', (event) => {
      // Only handle navigation messages from our Rails app
      if (event.data.source !== 'llamapress-navigation') return;

      if (event.data.type === 'before-navigate') {
        // The Rails app is about to navigate - save the current path to history
        const fromPath = event.data.fromPath;
        const toPath = event.data.toPath;

        if (fromPath && toPath && fromPath !== toPath) {
          // Avoid duplicates at the top of the stack
          if (this.navigationHistory.length === 0 ||
              this.navigationHistory[this.navigationHistory.length - 1] !== fromPath) {
            this.navigationHistory.push(fromPath);
          }
        }

        // Update URL bar immediately when navigation starts
        if (this.urlInput && toPath) {
          this.urlInput.value = toPath;
        }

        // Track current path for reliable refresh fallback
        if (toPath) {
          this.currentPath = toPath;
        }
      } else if (event.data.type === 'page-loaded') {
        // Update URL display when Rails app loads a new page
        if (this.urlInput && event.data.path) {
          this.urlInput.value = event.data.path;
        }

        // Track current path for reliable refresh fallback
        if (event.data.path) {
          this.currentPath = event.data.path;
        }
      }
    });
  }

  /**
   * Helper method for scoped queries with fallback to global
   */
  querySelector(selector) {
    return this.container.querySelector(selector);
  }

  /**
   * Helper method for scoped querySelectorAll with fallback to global
   */
  querySelectorAll(selector) {
    return this.container.querySelectorAll(selector);
  }

  /**
   * Initialize iframe sources based on environment
   */
  initIframeSources() {
    // Set Rails iframe URL
    if (this.liveSiteFrame) {
      this.liveSiteFrame.src = getRailsUrl();
    }

    // Set VS Code iframe URL
    if (this.vsCodeFrame) {
      this.vsCodeFrame.src = getVSCodeUrl();
    }

    // Set Tickets iframe URL
    if (this.ticketsFrame) {
      this.ticketsFrame.src = getTicketsUrl();
    }

    // Set Feedback iframe URL
    if (this.feedbackFrame) {
      this.feedbackFrame.src = getFeedbackUrl();
    }
  }

  // ============================================================================
  // STREAMING PREVIEW Methods (contentFrame - for HTML generation preview)
  // ============================================================================

  /**
   * Flush HTML content to STREAMING PREVIEW iframe
   * Used when agent is generating HTML and we want to show it token-by-token
   */
  flushToStreamingPreview(htmlContent) {
    if (!this.contentFrame) return;

    try {
      const iframeDoc = this.contentFrame.contentDocument || this.contentFrame.contentWindow.document;

      if (iframeDoc) {
        iframeDoc.open();
        iframeDoc.write(htmlContent);
        iframeDoc.close();

        // Auto-scroll iframe to bottom
        setTimeout(() => {
          if (iframeDoc.documentElement) {
            iframeDoc.documentElement.scrollTop = iframeDoc.documentElement.scrollHeight;
          }
          if (iframeDoc.body) {
            iframeDoc.body.scrollTop = iframeDoc.body.scrollHeight;
          }
        }, 100);
      }
    } catch (e) {
      // Error during streaming preview update (expected during streaming)
    }
  }

  /**
   * Create streaming overlay with animation
   * Used during HTML generation to show progress animation
   */
  createStreamingOverlay({ showCloseButton = false, text = 'Your Page is Being Built!' } = {}) {
    // Check if overlay already exists
    if (document.getElementById('streamingOverlay')) {
      return;
    }

    const browserContent = document.querySelector('.browser-content');
    if (!browserContent) return;

    // Create overlay div
    const overlay = document.createElement('div');
    overlay.id = 'streamingOverlay';
    overlay.style.position = 'absolute';
    overlay.style.top = '0';
    overlay.style.left = '0';
    overlay.style.width = '100%';
    overlay.style.height = '100%';
    overlay.style.background = 'rgba(0, 0, 0, 0.4)';
    overlay.style.display = 'flex';
    overlay.style.flexDirection = 'column';
    overlay.style.alignItems = 'center';
    overlay.style.justifyContent = 'flex-start';
    overlay.style.zIndex = '10';
    overlay.style.borderRadius = '8px';
    overlay.style.overflow = 'hidden';
    overlay.style.paddingTop = '24px';

    // Add close button if requested
    if (showCloseButton) {
      const closeBtn = document.createElement('button');
      closeBtn.innerHTML = '&times;';
      closeBtn.style.position = 'absolute';
      closeBtn.style.top = '10px';
      closeBtn.style.right = '10px';
      closeBtn.style.background = 'rgba(255, 255, 255, 0.2)';
      closeBtn.style.border = 'none';
      closeBtn.style.color = 'white';
      closeBtn.style.fontSize = '2rem';
      closeBtn.style.cursor = 'pointer';
      closeBtn.style.borderRadius = '50%';
      closeBtn.style.width = '40px';
      closeBtn.style.height = '40px';
      closeBtn.style.display = 'flex';
      closeBtn.style.alignItems = 'center';
      closeBtn.style.justifyContent = 'center';
      closeBtn.style.lineHeight = '1';
      closeBtn.style.zIndex = '11';
      closeBtn.addEventListener('mouseenter', () => { closeBtn.style.background = 'rgba(255, 255, 255, 0.4)'; });
      closeBtn.addEventListener('mouseleave', () => { closeBtn.style.background = 'rgba(255, 255, 255, 0.2)'; });
      closeBtn.addEventListener('click', () => this.removeStreamingOverlay());
      overlay.appendChild(closeBtn);
    }

    // Create text. Keep it on a single line (no ugly wrap in a narrow preview):
    // if the title starts with "Your ", that prefix lives in its own span so we
    // can drop just "Your" when the pane is too small, restoring it when it fits.
    const overlayText = document.createElement('div');
    overlayText.style.color = 'white';
    overlayText.style.fontSize = '1.8rem';
    overlayText.style.fontWeight = 'bold';
    overlayText.style.fontFamily = 'Arial, sans-serif';
    overlayText.style.textShadow = '2px 2px 4px rgba(0,0,0,0.5)';
    overlayText.style.whiteSpace = 'nowrap';

    const titlePrefix = 'Your ';
    if (text.startsWith(titlePrefix)) {
      const prefixSpan = document.createElement('span');
      prefixSpan.className = 'overlay-title-prefix';
      prefixSpan.textContent = titlePrefix;
      overlayText.appendChild(prefixSpan);
      overlayText.appendChild(document.createTextNode(text.slice(titlePrefix.length)));
    } else {
      overlayText.textContent = text;
    }

    const textContainer = document.createElement('div');
    textContainer.style.width = 'auto';
    textContainer.style.textAlign = 'center';
    textContainer.style.overflow = 'hidden';
    textContainer.appendChild(overlayText);

    // Create Lottie container. The animation is big & centered before a plan
    // exists, then shrinks to the top once the todo list takes over the pane.
    const lottieContainer = document.createElement('div');
    lottieContainer.id = 'lottieAnimation';
    lottieContainer.style.width = '100%';
    lottieContainer.style.display = 'flex';
    lottieContainer.style.alignItems = 'center';
    lottieContainer.style.justifyContent = 'center';

    // Load Lottie script if needed
    if (!document.querySelector('script[src*="lottie-player"]')) {
      const lottieScript = document.createElement('script');
      lottieScript.src = "https://unpkg.com/@dotlottie/player-component@latest/dist/dotlottie-player.js";
      document.head.appendChild(lottieScript);
    }

    // Create Lottie player
    const lottiePlayer = document.createElement('dotlottie-player');
    lottiePlayer.src = "https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/hffa8kqjfn9yzfx28pogpvqhn7cd";
    lottiePlayer.background = "transparent";
    lottiePlayer.speed = "1";
    lottiePlayer.setAttribute("autoplay", "");
    lottiePlayer.setAttribute("loop", "");
    lottieContainer.appendChild(lottiePlayer);

    // Cycling tips under the title while Leo gets started (pre-plan only). One at
    // a time, fading between three short hints, ~35% of the title size. Icons are
    // the real toolbar icons (Font Awesome 6.5.1, loaded globally) so they match.
    const tipsContainer = document.createElement('div');
    tipsContainer.style.width = 'auto';
    tipsContainer.style.maxWidth = '100%';
    tipsContainer.style.textAlign = 'center';
    tipsContainer.style.padding = '4px 0 0';
    tipsContainer.style.boxSizing = 'border-box';
    tipsContainer.style.color = 'rgba(255, 255, 255, 0.8)';
    tipsContainer.style.fontFamily = 'Arial, sans-serif';
    tipsContainer.style.fontSize = '0.7rem';
    tipsContainer.style.fontWeight = 'bold';
    tipsContainer.style.textShadow = '1px 1px 2px rgba(0,0,0,0.5)';

    const tips = [
      { icon: 'fa-mouse-pointer', text: 'Help Leo by pointing to an element' },
      { icon: 'fa-paperclip', text: 'Upload files to include in your app' },
      { icon: 'fa-forward', text: "Switching to Plan Mode can improve Leo's performance" },
      { icon: 'fa-lightbulb', text: 'view more tips', href: 'https://llamapress.ai/wiki' },
    ];
    const tipEl = document.createElement('div');
    tipEl.style.display = 'inline-flex';
    tipEl.style.alignItems = 'center';
    tipEl.style.gap = '6px';
    tipEl.style.transition = 'opacity 0.4s ease';
    tipEl.style.opacity = '1';
    const renderTip = (i) => {
      const t = tips[i];
      // Each tip is prefixed with "Tip:" so the user knows it's a tip.
      const body = t.href
        ? `<a href="${t.href}" target="_blank" rel="noopener" style="color:inherit;text-decoration:underline;">${t.text}</a>`
        : t.text;
      tipEl.innerHTML = `<i class="fa-solid ${t.icon}"></i><span>Tip: ${body}</span>`;
    };
    tipsContainer.appendChild(tipEl);

    // Cycle the tips in a shuffled order so it's different every time; reshuffle
    // each pass (avoiding an immediate repeat) so every tip still gets shown.
    const shuffle = (arr) => {
      const a = arr.slice();
      for (let i = a.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [a[i], a[j]] = [a[j], a[i]];
      }
      return a;
    };
    let order = shuffle(tips.map((_, i) => i));
    let pos = 0;
    renderTip(order[pos]);

    this._overlayTipInterval = setInterval(() => {
      tipEl.style.opacity = '0';
      setTimeout(() => {
        pos++;
        if (pos >= order.length) {
          const last = order[order.length - 1];
          order = shuffle(order);
          if (order.length > 1 && order[0] === last) {
            [order[0], order[order.length - 1]] = [order[order.length - 1], order[0]];
          }
          pos = 0;
        }
        renderTip(order[pos]);
        tipEl.style.opacity = '1';
      }, 400);
    }, 20000);

    // Cloned todo list panel (#0d0d1a box) — only shown once a plan exists.
    const todoContainer = document.createElement('div');
    todoContainer.id = 'overlayTodoList';
    todoContainer.style.flex = '1 1 auto';
    todoContainer.style.width = '100%';
    todoContainer.style.maxWidth = '480px';
    todoContainer.style.minHeight = '0';
    todoContainer.style.overflowY = 'auto';
    todoContainer.style.marginTop = '8px';
    todoContainer.style.marginBottom = '16px';
    todoContainer.style.boxSizing = 'border-box';
    todoContainer.style.background = '#0d0d1a';
    todoContainer.style.borderRadius = '10px';
    todoContainer.style.border = '1px solid rgba(255, 255, 255, 0.08)';
    todoContainer.style.padding = '16px 20px';
    todoContainer.style.display = 'none';

    // Toggle the two overlay layouts:
    //   building → big centered animation + cycling tips, no box
    //   plan     → small animation up top + the cloned todo list box
    const setOverlayMode = (mode) => {
      const isPlan = mode === 'plan';
      // Building: title + tips + ball are centered as a group (animation doesn't
      // grow). Plan: top-aligned with the todo box filling the space below.
      overlay.style.justifyContent = isPlan ? 'flex-start' : 'center';
      lottieContainer.style.flex = '0 0 auto';
      // Title is large while building, then shrinks once the todo list takes over.
      overlayText.style.fontSize = isPlan ? '1.8rem' : '2.5rem';
      // Tips track ~35% of the current title size (bigger pre-plan, smaller after).
      tipsContainer.style.fontSize = isPlan ? '0.63rem' : '0.875rem';
      lottiePlayer.style.width = isPlan ? '140px' : '240px';
      lottiePlayer.style.height = isPlan ? '140px' : '240px';
      // Tips keep cycling in the pill in both states (incl. after the todo list).
      tipsContainer.style.display = 'block';
      todoContainer.style.display = isPlan ? 'block' : 'none';
      // Re-evaluate the "Your " drop since the title size just changed.
      this._fitOverlayTitle?.();
    };
    this._setOverlayMode = setOverlayMode;
    setOverlayMode('building'); // start in the building state

    // Wrap the title + tips in a semi-opaque "pill" so the white text reads
    // clearly over the live site behind the overlay, without darkening the rest.
    const headerBox = document.createElement('div');
    headerBox.style.flex = '0 0 auto';
    headerBox.style.display = 'flex';
    headerBox.style.flexDirection = 'column';
    headerBox.style.alignItems = 'center';
    headerBox.style.maxWidth = '92%';
    headerBox.style.boxSizing = 'border-box';
    headerBox.style.padding = '12px 26px';
    headerBox.style.borderRadius = '14px';
    headerBox.style.background = 'rgba(0, 0, 0, 0.45)';
    headerBox.appendChild(textContainer);
    headerBox.appendChild(tipsContainer);

    overlay.appendChild(headerBox);
    overlay.appendChild(lottieContainer);
    overlay.appendChild(todoContainer);
    browserContent.appendChild(overlay);

    this.overlayElement = overlay;

    // Keep the title on one line: drop the "Your " prefix when the pane is too
    // narrow to fit the full title, and restore it when there's room again.
    const prefixSpan = overlayText.querySelector('.overlay-title-prefix');
    if (prefixSpan) {
      const fitTitle = () => {
        prefixSpan.style.display = 'inline';            // try the full title first
        // Measure against the pane width (minus the pill's padding/margins), not
        // the now content-hugging title container.
        const available = browserContent.clientWidth - 70;
        if (overlayText.scrollWidth > available) {
          prefixSpan.style.display = 'none';            // too tight — drop "Your"
        }
      };
      this._fitOverlayTitle = fitTitle; // let setOverlayMode re-fit after size changes
      requestAnimationFrame(fitTitle); // measure once layout is settled
      const titleObserver = new ResizeObserver(fitTitle);
      titleObserver.observe(browserContent);
      this._overlayTitleObserver = titleObserver;
    }

    // Mirror the chat's plan into the overlay and switch from the building layout
    // to the todo-list layout the moment a plan is created.
    this._startOverlayPlanMirror();
  }

  /**
   * Keep the building overlay in sync with the chat:
   *   • No plan yet → building layout (big centered animation + cycling tips).
   *   • Plan (todo list) exists → clone the real chat plan node into the box,
   *     which guarantees identical styling and syncs regardless of streaming path.
   */
  _startOverlayPlanMirror() {
    const history = document.querySelector('[data-llamabot="message-history"]');
    if (!history) return;

    let scheduled = false;
    let lastHtml = '';

    const doMirror = () => {
      scheduled = false;
      const container = document.getElementById('overlayTodoList');
      if (!container) return; // overlay gone

      // Latest plan in the chat (exclude any clone living in the overlay itself)
      const plans = Array.from(history.querySelectorAll('.plan-modern'));
      const latest = plans[plans.length - 1];

      if (!latest) {
        // No plan yet → building layout; clear any stale clone.
        if (lastHtml !== '') { lastHtml = ''; container.innerHTML = ''; }
        this._setOverlayMode?.('building');
        return;
      }

      // Plan exists → swap to the todo-list layout and clone the real plan node.
      this._setOverlayMode?.('plan');
      const html = latest.outerHTML;
      if (html === lastHtml) return; // unchanged, skip redundant DOM write
      lastHtml = html;

      const clone = latest.cloneNode(true);
      // Neutralize interactive bits so the read-only clone can't collide with the
      // chat copy (duplicate ids) or toggle the wrong plan when clicked.
      clone.removeAttribute('data-plan-id');
      clone.querySelectorAll('[id]').forEach(el => el.removeAttribute('id'));
      clone.querySelectorAll('[onclick]').forEach(el => el.removeAttribute('onclick'));
      // Always show the full task list in the overlay, even if collapsed in chat.
      clone.querySelectorAll('.plan-tasks-list').forEach(el => { el.style.display = 'block'; });

      container.innerHTML = '';
      container.appendChild(clone);
    };

    const schedule = () => {
      if (scheduled) return;
      scheduled = true;
      requestAnimationFrame(doMirror);
    };

    this._overlayObservers = [];
    const planObserver = new MutationObserver(schedule);
    planObserver.observe(history, { childList: true, subtree: true, characterData: true, attributes: true });
    this._overlayObservers.push(planObserver);

    // Mirror immediately in case a plan already exists when the overlay opens.
    doMirror();
  }

  /**
   * Stop mirroring the chat into the overlay.
   */
  _stopOverlayPlanMirror() {
    if (this._overlayObservers) {
      this._overlayObservers.forEach(o => o.disconnect());
      this._overlayObservers = null;
    }
  }

  /**
   * Remove streaming overlay
   */
  removeStreamingOverlay() {
    this._stopOverlayPlanMirror();
    if (this._overlayTitleObserver) {
      this._overlayTitleObserver.disconnect();
      this._overlayTitleObserver = null;
    }
    this._fitOverlayTitle = null;
    if (this._overlayTipInterval) {
      clearInterval(this._overlayTipInterval);
      this._overlayTipInterval = null;
    }
    this._setOverlayMode = null;
    const overlay = document.getElementById('streamingOverlay');
    if (overlay) {
      overlay.remove();
      this.overlayElement = null;
    }
  }

  // ============================================================================
  // RAILS APP PREVIEW Methods (liveSiteFrame - for live Rails app)
  // ============================================================================

  /**
   * Refresh the Rails app preview iframe
   * Only refreshes the "Your App" iframe (liveSiteFrame), not other iframes like VSCode
   *
   * @param {Function} getRailsDebugInfoCallback - Function that accepts a callback parameter
   *                                                The callback will receive debugInfoJson
   *
   * Example usage:
   *   iframeManager.refreshRailsApp((callback) => this.getRailsDebugInfo(callback))
   */
  refreshRailsApp(getRailsDebugInfoCallback) {
    // Only refresh the Rails app iframe (liveSiteFrame), not all iframes
    if (!this.liveSiteFrame) return;

    const isRailsIFrame = this.liveSiteFrame.src.includes(':3000') || this.liveSiteFrame.src.includes('https://rails-');

    if (isRailsIFrame) {
      getRailsDebugInfoCallback((debugInfoJson) => {
        if (this.liveSiteFrame.src) {
          let additionalRequestPath = debugInfoJson.request_path;

          // Use tracked currentPath as fallback when iframe query fails (e.g., 500 error)
          if (!additionalRequestPath || debugInfoJson instanceof Error) {
            console.warn('Warning: debugInfoJson.request_path is undefined! Using tracked path as fallback.', debugInfoJson);
            additionalRequestPath = this.currentPath || '/';
          }

          this.liveSiteFrame.src = getRailsUrl() + additionalRequestPath;
        }
      });
    }
  }

  /**
   * Refresh the Tickets iframe
   */
  refreshTicketsFrame() {
    if (this.ticketsFrame && this.ticketsFrame.src) {
      this.ticketsFrame.src = this.ticketsFrame.src;
    }
  }

  /**
   * Refresh the Feedback iframe
   */
  refreshFeedbackFrame() {
    if (this.feedbackFrame && this.feedbackFrame.src) {
      this.feedbackFrame.src = this.feedbackFrame.src;
    }
  }

  /**
   * Simple iframe refresh (legacy - for streaming preview)
   * @deprecated Use flushToStreamingPreview instead
   */
  refreshIframe() {
    if (!this.contentFrame) return;

    setTimeout(() => {
      this.contentFrame.src = this.contentFrame.src;
    }, 100);
  }

  /**
   * Navigate the Rails iframe to a specific path
   * @param {string} path - The path to navigate to (e.g., '/users', '/posts/123')
   * @param {boolean} addToHistory - Whether to add this navigation to history (default: true)
   */
  navigateToPath(path, addToHistory = true) {
    if (!this.liveSiteFrame) return;

    // Ensure path starts with /
    if (!path.startsWith('/')) {
      path = '/' + path;
    }

    // Save current path to history before navigating (for back button)
    if (addToHistory) {
      const currentPath = this.extractRelativePath(this.liveSiteFrame.src);
      if (currentPath && currentPath !== path) {
        this.navigationHistory.push(currentPath);
      }
    }

    // Update iframe src
    this.liveSiteFrame.src = getRailsUrl() + path;

    // Track current path for reliable refresh fallback
    this.currentPath = path;

    // Update URL input
    if (this.urlInput) {
      this.urlInput.value = path;
    }
  }

  /**
   * Navigate back in the iframe history
   * Uses our own history stack since cross-origin iframes don't allow history access
   */
  navigateBack() {
    if (this.navigationHistory.length > 0) {
      const previousPath = this.navigationHistory.pop();
      this.navigateToPath(previousPath, false); // Don't add to history when going back
    }
  }

  /**
   * Extract relative path from iframe URL
   * @param {string} url - Full URL from iframe
   * @returns {string} - Relative path (e.g., '/users')
   */
  extractRelativePath(url) {
    try {
      const urlObj = new URL(url);
      return urlObj.pathname || '/';
    } catch (e) {
      return '/';
    }
  }

  /**
   * Update URL input to show current iframe path
   */
  updateUrlDisplay() {
    if (!this.liveSiteFrame || !this.urlInput) return;

    try {
      const iframeSrc = this.liveSiteFrame.src;
      const relativePath = this.extractRelativePath(iframeSrc);
      this.urlInput.value = relativePath;
    } catch (e) {
      // Could not update URL display
    }
  }

  /**
   * Fetch available routes from the backend
   */
  async fetchRoutes() {
    if (this.cachedRoutes) {
      return this.cachedRoutes;
    }

    try {
      const response = await fetch('/rails-routes');
      const data = await response.json();
      this.cachedRoutes = data.routes || [];
      return this.cachedRoutes;
    } catch (e) {
      console.error('Error fetching routes:', e);
      return [{ path: '/', name: 'Home' }];
    }
  }

  /**
   * Show the URL dropdown with available routes
   */
  async showUrlDropdown() {
    if (!this.urlDropdown) return;

    const routes = await this.fetchRoutes();

    // Clear existing dropdown content
    this.urlDropdown.innerHTML = '';

    // Populate dropdown with routes
    routes.forEach(route => {
      const item = document.createElement('div');
      item.className = 'url-dropdown-item';
      item.innerHTML = `
        <span class="url-dropdown-path">${route.path}</span>
        <span class="url-dropdown-name">${route.name}</span>
      `;

      item.addEventListener('click', () => {
        this.navigateToPath(route.path);
        this.hideUrlDropdown();
      });

      this.urlDropdown.appendChild(item);
    });

    // Show dropdown
    this.urlDropdown.classList.remove('hidden');
  }

  /**
   * Hide the URL dropdown
   */
  hideUrlDropdown() {
    if (!this.urlDropdown) return;
    this.urlDropdown.classList.add('hidden');
  }

  /**
   * Initialize URL navigation functionality
   */
  initUrlNavigation() {
    if (!this.urlInput) return;

    // Show dropdown when input is focused/clicked
    this.urlInput.addEventListener('focus', () => {
      this.showUrlDropdown();
    });

    this.urlInput.addEventListener('click', () => {
      this.showUrlDropdown();
    });

    // Hide dropdown when clicking outside
    document.addEventListener('click', (e) => {
      if (!this.urlInput.contains(e.target) && !this.urlDropdown?.contains(e.target)) {
        this.hideUrlDropdown();
      }
    });

    // Handle Enter key to navigate
    this.urlInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const path = this.urlInput.value.trim();
        this.navigateToPath(path);
        this.urlInput.blur(); // Remove focus after navigation
        this.hideUrlDropdown();
      } else if (e.key === 'Escape') {
        this.hideUrlDropdown();
        this.urlInput.blur();
      }
    });

    // Update URL display when iframe loads
    if (this.liveSiteFrame) {
      this.liveSiteFrame.addEventListener('load', () => {
        this.updateUrlDisplay();
      });
    }

    // Initialize with current path
    this.updateUrlDisplay();

    // Pre-fetch routes for faster dropdown display
    this.fetchRoutes();
  }

  // ============================================================================
  // UI Controls (for both iframe types)
  // ============================================================================

  /**
   * Initialize navigation buttons (refresh, back, etc.)
   */
  initNavigationButtons() {
    // Refresh button
    const refreshButton = this.querySelector('[data-llamabot="refresh-button"]');
    if (refreshButton) {
      refreshButton.addEventListener('click', (e) => {
        const button = e.currentTarget;
        const svg = button.querySelector('svg');

        if (svg) {
          svg.style.animation = 'spin 0.5s linear';
          setTimeout(() => {
            svg.style.animation = '';
          }, 500);
        }

        // Emit refresh event for other components to handle
        window.dispatchEvent(new CustomEvent('iframeRefreshRequested'));
      });
    }

    // Back button - uses our own history stack since cross-origin iframes don't allow history access
    const backButton = this.querySelector('[data-llamabot="back-button"]');
    if (backButton && this.liveSiteFrame) {
      backButton.addEventListener('click', () => {
        if (this.navigationHistory.length > 0) {
          this.navigateBack();
        } else {
          // No history available - provide visual feedback
          backButton.style.transform = 'scale(0.9)';
          backButton.style.opacity = '0.5';
          setTimeout(() => {
            backButton.style.transform = '';
            backButton.style.opacity = '';
          }, 150);
        }
      });
    }
  }

  /**
   * Init tab switching
   */
  initTabSwitching() {
    const tabs = this.querySelectorAll('.tab');
    const iframes = this.querySelectorAll('.content-iframe');

    // Map old ID names to new data-llamabot attribute names
    const idToDataAttrMap = {
      'liveSiteFrame': 'live-site-frame',
      'vsCodeFrame': 'vscode-frame',
      'ticketsFrame': 'tickets-frame',
      'feedbackFrame': 'feedback-frame',
      'contentFrame': 'content-frame',
      'gitFrame': 'git-frame',
      'logsFrame': 'logs-frame',
      'pgWebFrame': 'pgweb-frame'
    };

    tabs.forEach(tab => {
      tab.addEventListener('click', (e) => {
        // Don't switch tabs if clicking the external link button
        if (e.target.closest('.tab-external-link')) {
          return;
        }

        tabs.forEach(t => t.classList.remove('active'));
        iframes.forEach(i => i.classList.remove('active'));

        tab.classList.add('active');
        const targetIframeId = tab.dataset.target;

        // Map old ID to new data-llamabot attribute
        const dataAttrName = idToDataAttrMap[targetIframeId] || targetIframeId;
        const targetIframe = this.querySelector(`[data-llamabot="${dataAttrName}"]`);

        if (targetIframe) {
          targetIframe.classList.add('active');
        }
      });
    });

    // Initialize external link buttons
    this.initExternalLinkButtons();
  }

  /**
   * Initialize external link buttons on tabs
   */
  initExternalLinkButtons() {
    const externalLinkButtons = this.querySelectorAll('.tab-external-link');

    // Map old ID names to new data-llamabot attribute names
    const idToDataAttrMap = {
      'liveSiteFrame': 'live-site-frame',
      'vsCodeFrame': 'vscode-frame',
      'ticketsFrame': 'tickets-frame',
      'feedbackFrame': 'feedback-frame',
      'contentFrame': 'content-frame',
      'gitFrame': 'git-frame',
      'logsFrame': 'logs-frame',
      'pgWebFrame': 'pgweb-frame'
    };

    externalLinkButtons.forEach(button => {
      button.addEventListener('click', (e) => {
        e.stopPropagation(); // Prevent tab switching

        const iframeId = button.dataset.iframe;

        // Map old ID to new data-llamabot attribute
        const dataAttrName = idToDataAttrMap[iframeId] || iframeId;
        const iframe = this.querySelector(`[data-llamabot="${dataAttrName}"]`);

        if (iframe && iframe.src) {
          // Open the iframe's current URL in a new tab
          window.open(iframe.src, '_blank');
        }
      });
    });
  }

  /**
   * Init view mode toggle
   */
  initViewModeToggle() {
    const desktopModeBtn = this.querySelector('[data-llamabot="desktop-mode-btn"]');
    const mobileModeBtn = this.querySelector('[data-llamabot="mobile-mode-btn"]');
    const browserContent = this.querySelector('.browser-content');

    if (!desktopModeBtn || !mobileModeBtn || !browserContent) return;

    desktopModeBtn.addEventListener('click', () => {
      browserContent.classList.remove('mobile-view');
      desktopModeBtn.classList.add('active');
      mobileModeBtn.classList.remove('active');
    });

    mobileModeBtn.addEventListener('click', () => {
      browserContent.classList.add('mobile-view');
      mobileModeBtn.classList.add('active');
      desktopModeBtn.classList.remove('active');
    });
  }
}
