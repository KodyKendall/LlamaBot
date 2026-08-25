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

import { getRailsUrl, getVSCodeUrl, getInboxUrl, getActivityUrl, DEFAULT_CONFIG } from '../config.js';
import { isTabVisible } from '../utils/tabVisibility.js';
import { loadOverlayAds, OverlayAdRotator, evaluatePolicy, policyAllowsMode, recordShown } from './OverlayAds.js';
import { installOverlayDevtools } from './OverlayDevtools.js';

/**
 * data-target name → data-llamabot attribute, for every tab in the browser pane.
 *
 * Declared once at module scope: this map used to be duplicated inline in both
 * initTabSwitching() and initExternalLinkButtons(), and a tab added to one copy
 * but not the other silently loses either its click target or its pop-out.
 */
const TAB_TARGET_TO_FRAME = {
  'liveSiteFrame': 'live-site-frame',
  'vsCodeFrame': 'vscode-frame',
  'inboxFrame': 'inbox-frame',
  'activityFrame': 'activity-frame',
  'contentFrame': 'content-frame',
  'gitFrame': 'git-frame',
  'logsFrame': 'logs-frame',
  'pgWebFrame': 'pgweb-frame'
};

export class IframeManager {
  constructor(container = null) {
    this.container = container || document;

    // STREAMING PREVIEW iframe (for HTML generation preview)
    this.contentFrame = this.querySelector('[data-llamabot="content-frame"]');

    // RAILS APP PREVIEW iframe (for live Rails app)
    this.liveSiteFrame = this.querySelector('[data-llamabot="live-site-frame"]');

    // VS CODE iframe
    this.vsCodeFrame = this.querySelector('[data-llamabot="vscode-frame"]');

    // INBOX iframe (messages, tickets, feedback, requests, notifications)
    this.inboxFrame = this.querySelector('[data-llamabot="inbox-frame"]');

    // ACTIVITY iframe (audit log / record history)
    this.activityFrame = this.querySelector('[data-llamabot="activity-frame"]');

    // Unread-messages badge on the Messages tab
    this.messagesUnreadBadge = this.querySelector('[data-llamabot="messages-unread-badge"]');

    // URL input element
    this.urlInput = this.querySelector('[data-llamabot="url-input"]');

    // URL dropdown element
    this.urlDropdown = this.querySelector('[data-llamabot="url-dropdown"]');

    // Cached routes
    this.cachedRoutes = null;

    this.overlayElement = null;

    // Navigation history stack for back button (since we can't access cross-origin iframe history)
    this.navigationHistory = [];

    // window.leoAds — console handle for driving the overlay without an agent turn.
    // Installed here (rather than off window.chatApp) so it binds to this manager
    // directly and doesn't care when/whether chatApp gets assigned.
    installOverlayDevtools(this);

    // Track current path for reliable refresh (fallback when iframe query fails).
    // Seeded from the last page this browser was on so a full page refresh puts
    // the user back where they were instead of bouncing them to the app root.
    this.currentPath = this._savedRailsPath() || '/';

    // Initialize iframe URLs
    this.initIframeSources();

    // Listen for navigation messages from the Rails iframe
    this.initNavigationListener();

    // Listen for unread-count pushes from the Rails messages iframe
    this.initUnreadBadgeListener();

    // Escape dismisses the building overlay
    this.initOverlayEscapeListener();
  }

  // ============================================================================
  // Session restore (remember the last previewed page + tab across a refresh)
  // ============================================================================

  /**
   * Storage key for a remembered value, scoped to the Rails origin.
   *
   * Scoping matters: a user with two boxes open in the same browser must not
   * inherit the other app's last page, and an unscoped key would do exactly that.
   */
  _storageKey(kind) {
    return `llamabot:${kind}:${getRailsUrl()}`;
  }

  /**
   * Read a remembered value. localStorage can throw outright (Safari private
   * mode, blocked third-party storage when the chat is embedded), and a dead
   * storage must never take the iframe down with it — hence the swallow.
   */
  _readStored(kind) {
    try {
      return window.localStorage.getItem(this._storageKey(kind));
    } catch (e) {
      return null;
    }
  }

  _writeStored(kind, value) {
    try {
      window.localStorage.setItem(this._storageKey(kind), value);
    } catch (e) {
      // Storage unavailable or full — restore is a nicety, never a hard failure.
    }
  }

  /**
   * Sanitize a remembered path before it can become an iframe src.
   *
   * Only a same-origin absolute path survives. A full URL, a protocol-relative
   * "//evil.com" (or its "/\evil.com" cousin, which browsers normalize the same
   * way), or control characters would all point the preview at someone else's
   * origin — so anything that isn't a plain "/path" is dropped.
   *
   * Returns '' when there is nothing worth restoring, which callers append to
   * the base URL to get byte-identical behavior to the pre-restore code.
   */
  _safeRestorePath(path) {
    if (typeof path !== 'string') return '';
    if (path.charAt(0) !== '/') return '';
    if (path.charAt(1) === '/' || path.charAt(1) === '\\') return '';
    if (path === '/') return '';                 // root is already the default
    if (/[\x00-\x20\x7f]/.test(path)) return ''; // control chars / whitespace
    return path;
  }

  /**
   * The last Rails path this browser was on, or '' if there's nothing safe to
   * restore.
   */
  _savedRailsPath() {
    return this._safeRestorePath(this._readStored('lastPath'));
  }

  /**
   * Remember the page the preview is on, so the next full page load can return
   * to it. Navigating back to the root is stored explicitly ('/') so it clears
   * a stale deep link rather than silently keeping it.
   */
  _rememberPath(path) {
    this._writeStored('lastPath', this._safeRestorePath(path) || '/');
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
          this._rememberPath(toPath);
        }
      } else if (event.data.type === 'page-loaded') {
        // Update URL display when Rails app loads a new page
        if (this.urlInput && event.data.path) {
          this.urlInput.value = event.data.path;
        }

        // Track current path for reliable refresh fallback
        if (event.data.path) {
          this.currentPath = event.data.path;
          this._rememberPath(event.data.path);
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
      this.liveSiteFrame.src = this._railsSrcWithAuth();
    }

    // Show the restored page in the URL bar right away. Without this the bar
    // reads "/" until the Rails app posts its page-loaded message back.
    if (this.urlInput && this.currentPath) {
      this.urlInput.value = this.currentPath;
    }

    // Set VS Code iframe URL. Skipped when the Code tab is switched off: the
    // editor container is off by default, so loading the frame would point the
    // browser at a stopped editor.
    const visibleTabs = (typeof window !== 'undefined' && window.LLAMABOT_VISIBLE_TABS) || null;
    if (this.vsCodeFrame && isTabVisible('vsCodeFrame', visibleTabs)) {
      this.vsCodeFrame.src = getVSCodeUrl();
    }

    // Set Inbox iframe URL. Loaded eagerly, which is what keeps the unread
    // badge live: the poller that feeds it runs inside this frame (the Rails
    // inbox layout renders it on every inbox page), so the frame has to be
    // loaded before the tab is ever opened.
    if (this.inboxFrame) {
      this.inboxFrame.src = getInboxUrl();
    }

    // Set Activity iframe URL
    if (this.activityFrame) {
      this.activityFrame.src = getActivityUrl();
    }
  }

  /**
   * Resolve the initial Rails iframe src, threading a one-time Unified Login
   * grant to the Rails app when present.
   *
   * The box's /auth/consume redirect carries ?rails_token=<raw grant> so the
   * Rails app (Phase 3 gem middleware at /llamapress_auth/consume) can redeem
   * the SAME grant once more with audience=rails_app — that's what kills the
   * Devise login wall inside this iframe. We hand the token to the iframe, then
   * scrub it from the top-window URL bar (keeping prompt/llm_model/agent_mode)
   * so a refresh or share can't replay a spent grant.
   *
   * Both branches also thread the remembered last page: the plain branch appends
   * it to the base URL, the token branch hands it to the gem as return_to so the
   * post-login redirect lands there too. With nothing remembered the saved path
   * is '' / '/' and this returns getRailsUrl() verbatim — byte-identical to the
   * previous behavior, which is the entire backwards-compat story for old flows
   * (no token, no change). Until the Phase 3 gem ships, a threaded token just
   * 404s to the Rails login page, same as today's wall.
   */
  _railsSrcWithAuth() {
    const base = getRailsUrl();
    const savedPath = this._savedRailsPath();
    try {
      const params = new URLSearchParams(window.location.search);
      const token = params.get('rails_token');
      if (!token) return base + savedPath;

      const src = base + '/llamapress_auth/consume?token=' +
        encodeURIComponent(token) + '&return_to=' + encodeURIComponent(savedPath || '/');

      // Strip only rails_token from the URL bar; keep the chat hand-off params.
      params.delete('rails_token');
      const rest = params.toString();
      const cleaned = window.location.pathname +
        (rest ? '?' + rest : '') + window.location.hash;
      window.history.replaceState({}, '', cleaned);

      return src;
    } catch (e) {
      // Any parsing/replaceState failure must not break the iframe — fall back.
      return base + savedPath;
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

    this._stopOverlayAds();   // belt-and-braces: never inherit a previous overlay's rotator

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
    overlay.style.boxSizing = 'border-box';
    overlay.style.padding = '16px';
    overlay.style.gap = '12px';

    // Add close control if requested. A bare "×" reads as "cancel", which made
    // users unsure whether hitting it would stop Leo. Instead we show a labeled
    // "Hide" pill, and reveal a "Leo keeps building" reassurance on hover so the
    // user learns it's safe to dismiss the loading screen.
    if (showCloseButton) {
      const closeWrap = document.createElement('div');
      closeWrap.style.position = 'absolute';
      closeWrap.style.top = '10px';
      closeWrap.style.right = '10px';
      closeWrap.style.zIndex = '11';
      closeWrap.style.display = 'flex';
      closeWrap.style.flexDirection = 'column';
      closeWrap.style.alignItems = 'flex-end';
      closeWrap.style.gap = '4px';

      const closeBtn = document.createElement('button');
      closeBtn.innerHTML = 'Hide <span style="font-size:1.2rem;line-height:1;">&times;</span>';
      closeBtn.title = 'Leo will continue building';
      closeBtn.style.display = 'flex';
      closeBtn.style.alignItems = 'center';
      closeBtn.style.gap = '6px';
      closeBtn.style.background = 'rgba(255, 255, 255, 0.2)';
      closeBtn.style.border = 'none';
      closeBtn.style.color = 'white';
      closeBtn.style.fontSize = '0.9rem';
      closeBtn.style.fontWeight = 'bold';
      closeBtn.style.fontFamily = 'Arial, sans-serif';
      closeBtn.style.cursor = 'pointer';
      closeBtn.style.borderRadius = '20px';
      closeBtn.style.padding = '6px 12px';
      closeBtn.style.lineHeight = '1';

      // Reassurance hint, hidden until the user hovers the pill.
      const closeHint = document.createElement('div');
      closeHint.textContent = 'Leo will continue building';
      closeHint.style.color = 'rgba(255, 255, 255, 0.9)';
      closeHint.style.fontSize = '0.7rem';
      closeHint.style.fontFamily = 'Arial, sans-serif';
      closeHint.style.textShadow = '1px 1px 2px rgba(0,0,0,0.5)';
      closeHint.style.background = 'rgba(0, 0, 0, 0.35)';
      closeHint.style.borderRadius = '6px';
      closeHint.style.padding = '3px 8px';
      closeHint.style.whiteSpace = 'nowrap';
      closeHint.style.opacity = '0';
      closeHint.style.transition = 'opacity 0.2s ease';
      closeHint.style.pointerEvents = 'none';

      closeBtn.addEventListener('mouseenter', () => {
        closeBtn.style.background = 'rgba(255, 255, 255, 0.4)';
        closeHint.style.opacity = '1';
      });
      closeBtn.addEventListener('mouseleave', () => {
        closeBtn.style.background = 'rgba(255, 255, 255, 0.2)';
        closeHint.style.opacity = '0';
      });
      closeBtn.addEventListener('click', () => this.removeStreamingOverlay());

      closeWrap.appendChild(closeBtn);
      closeWrap.appendChild(closeHint);
      overlay.appendChild(closeWrap);
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

    // Render a title, keeping the optional "Your " prefix in its own span so the
    // fit logic can drop just that word in a narrow pane. Reused when the title
    // changes (e.g. to "Question from Leo" while Leo waits on the user, then back).
    const renderTitle = (titleText) => {
      overlayText.textContent = '';
      const titlePrefix = 'Your ';
      if (titleText.startsWith(titlePrefix)) {
        const prefixSpan = document.createElement('span');
        prefixSpan.className = 'overlay-title-prefix';
        prefixSpan.textContent = titlePrefix;
        overlayText.appendChild(prefixSpan);
        overlayText.appendChild(document.createTextNode(titleText.slice(titlePrefix.length)));
      } else {
        overlayText.textContent = titleText;
      }
    };
    renderTitle(text);
    this._overlayBaseTitle = text; // restore target after a question is answered
    this._setOverlayTitle = (t) => { renderTitle(t); this._fitOverlayTitle?.(); };

    const textContainer = document.createElement('div');
    textContainer.style.width = 'auto';
    textContainer.style.textAlign = 'left';
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
    tipsContainer.style.textAlign = 'left';
    // The status block is deliberately quiet now — it shares one compact top-left
    // row with the animation so the promo below it owns the pane. A little space
    // under the title still keeps the tip reading as its own thing.
    tipsContainer.style.padding = '6px 0 0';
    tipsContainer.style.boxSizing = 'border-box';
    tipsContainer.style.color = 'rgba(255, 255, 255, 0.85)';
    tipsContainer.style.fontFamily = 'Arial, sans-serif';
    tipsContainer.style.fontSize = '0.7rem';
    tipsContainer.style.fontWeight = 'normal';
    tipsContainer.style.textShadow = '1px 1px 2px rgba(0,0,0,0.5)';

    const tips = [
      { icon: 'fa-mouse-pointer', text: 'Help Leo by pointing to an element' },
      { icon: 'fa-paperclip', text: 'Upload files to include in your app' },
      { icon: 'fa-forward', text: "Switching to Plan Mode can improve Leo's performance" },
      { icon: 'fa-lightbulb', text: 'View Our Wiki for Guides & Tips to Use Leo', href: 'https://llamapress.ai/wiki' },
    ];
    const tipEl = document.createElement('div');
    tipEl.style.display = 'inline-flex';
    tipEl.style.alignItems = 'center';
    tipEl.style.gap = '7px';
    tipEl.style.transition = 'opacity 0.4s ease';
    tipEl.style.opacity = '1';
    // Wrap the tip in its own subtle pill so it stands apart from the solid title
    // above it, rather than blending into the same block of white text.
    tipEl.style.padding = '5px 13px';
    tipEl.style.borderRadius = '999px';
    tipEl.style.background = 'rgba(255, 255, 255, 0.08)';
    tipEl.style.border = '1px solid rgba(255, 210, 122, 0.28)';
    const renderTip = (i) => {
      const t = tips[i];
      // Each tip is prefixed with "Tip:" so the user knows it's a tip. Build the
      // node tree explicitly (rather than an innerHTML string) so the link is a
      // real <a> we can wire a click handler to. The anchor opens in a new tab
      // via target=_blank, and an explicit window.open() fallback guarantees the
      // new tab even if the default navigation is swallowed (e.g. when the chat
      // is embedded in another page).
      tipEl.innerHTML = '';
      const icon = document.createElement('i');
      icon.className = `fa-solid ${t.icon}`;
      icon.style.color = '#ffd27a'; // amber accent so the eye is drawn to the hint
      // Bold, amber "Tip:" label contrasts the big white title and flags the hint.
      const label = document.createElement('span');
      label.textContent = 'Tip:';
      label.style.color = '#ffd27a';
      label.style.fontWeight = '700';
      // Hint body sits at normal weight — lighter than both the title and the
      // label — so the three elements read as a clear hierarchy.
      const span = document.createElement('span');
      span.style.fontWeight = '400';
      if (t.href) {
        const link = document.createElement('a');
        link.href = t.href;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        link.textContent = t.text;
        link.style.color = 'inherit';
        link.style.textDecoration = 'underline';
        link.style.cursor = 'pointer';
        link.addEventListener('click', (e) => {
          e.preventDefault();
          e.stopPropagation();
          window.open(t.href, '_blank', 'noopener,noreferrer');
        });
        span.appendChild(link);
      } else {
        span.textContent = t.text;
      }
      tipEl.appendChild(icon);
      tipEl.appendChild(label);
      tipEl.appendChild(span);
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

    // Holds a live clone of the chat's question card while Leo is blocked waiting
    // on the user (AskUserQuestion / AskUserUIUXQuestion). Same #0d0d1a styling as
    // the todo box; only shown in 'question' mode.
    const questionContainer = document.createElement('div');
    questionContainer.id = 'overlayQuestionList';
    questionContainer.style.flex = '1 1 auto';
    questionContainer.style.width = '100%';
    questionContainer.style.maxWidth = '480px';
    questionContainer.style.minHeight = '0';
    questionContainer.style.overflowY = 'auto';
    questionContainer.style.marginTop = '12px';
    questionContainer.style.marginBottom = '16px';
    questionContainer.style.boxSizing = 'border-box';
    questionContainer.style.background = '#0d0d1a';
    questionContainer.style.borderRadius = '10px';
    questionContainer.style.border = '1px solid rgba(255, 255, 255, 0.08)';
    questionContainer.style.padding = '16px 20px';
    questionContainer.style.display = 'none';

    // Full-width rail that carries the status pill. It's what keeps the pill's
    // left edge flush with the promo below it: the rail is the same width as the
    // content column and centered like it, while the pill itself hugs its content
    // at the rail's left edge.
    const headerRow = document.createElement('div');
    headerRow.style.flex = '0 0 auto';
    headerRow.style.alignSelf = 'center';
    headerRow.style.width = '100%';
    headerRow.style.display = 'flex';
    headerRow.style.justifyContent = 'flex-start';

    // Jumbotron promo slot. The creative is mothership-owned HTML rendered in a
    // sandboxed iframe (see OverlayAds.js) — this is only the frame it sits in.
    // Stays hidden unless the mothership actually returns a promo, so an
    // unconfigured box looks exactly like it did before this shipped.
    const adContainer = document.createElement('div');
    adContainer.id = 'overlayAdSlot';
    adContainer.style.flex = '0 0 auto';
    adContainer.style.width = '100%';
    adContainer.style.maxWidth = '480px';
    adContainer.style.height = '140px';
    adContainer.style.marginBottom = '16px';
    adContainer.style.boxSizing = 'border-box';
    adContainer.style.background = 'rgba(13, 13, 26, 0.85)';
    adContainer.style.borderRadius = '10px';
    adContainer.style.border = '1px solid rgba(255, 255, 255, 0.08)';
    adContainer.style.overflow = 'hidden';
    adContainer.style.display = 'none';

    // Is a promo actually on screen right now? Everything about the overlay's
    // proportions hangs off this: with no promo the overlay must look EXACTLY
    // like it did before the jumbotron shipped (big centered animation under a
    // 2.5rem title), and it only shrinks into the compact top-left status pill
    // when there's a promo to make room for. A mothership that's down, empty, or
    // hasn't been configured yet is therefore invisible to the user.
    const adsVisible = () => {
      const mode = this._overlayMode;
      return !!this._overlayAdRotator
        && mode !== 'question'
        && policyAllowsMode(this._overlayAdPolicy, mode);
    };

    // The todo/question boxes are 480 wide; only the jumbotron gets the full 900.
    const columnWidth = () => (
      (this._overlayMode !== 'plan' && this._overlayMode !== 'question') ? '900px' : '480px'
    );

    // Sizes the promo slot for the current mode.
    //
    //   building → ALL of it. Nothing else is competing, and an ad that fills the
    //              pane is the whole point of the jumbotron.
    //   plan     → the payload's own `height` (default 140), so the todo list —
    //              which is what the user actually wants to watch — keeps the rest.
    //
    // Called from setOverlayMode (layout changed) and from the rotator's
    // onAdChange (a promo with a different height just came up).
    const applyAdSizing = this._applyOverlayAdSizing = () => {
      adContainer.style.maxWidth = columnWidth();
      if (this._overlayMode === 'plan') {
        const height = this._overlayAdRotator?.currentAd?.height || 140;
        adContainer.style.flex = `0 0 ${height}px`;
      } else {
        adContainer.style.flex = '1 1 auto';
      }
    };

    // Switches the whole status block between its two chromes:
    //
    //   classic (no promo) → the pre-jumbotron look. Title + tip centered in a
    //                        pill, the big animation centered below it, nothing
    //                        else on the pane.
    //   compact (promo up) → title + tip + a small ball in one quiet top-left
    //                        row, left-aligned to the promo's column, so the eye
    //                        goes to the promo instead.
    //
    // The animation actually moves between the two: a row item inside the pill in
    // compact chrome, its own full-width block under the pill in classic.
    const applyChrome = (withAd, { isPlan, isQuestion }) => {
      if (withAd) {
        overlay.style.padding = '16px';
        overlay.style.gap = '12px';
        headerRow.style.maxWidth = columnWidth();
        headerRow.style.justifyContent = 'flex-start';
        headerBox.style.flexDirection = 'row';
        headerBox.style.gap = '10px';
        headerBox.style.maxWidth = '100%';
        headerBox.style.padding = '8px 16px 8px 10px';
        textContainer.style.textAlign = 'left';
        tipsContainer.style.textAlign = 'left';
        tipsContainer.style.padding = '6px 0 0';
        lottieContainer.style.width = 'auto';
        // A row item inside the pill, ahead of the title/tip stack.
        if (headerBox.firstChild !== lottieContainer) headerBox.insertBefore(lottieContainer, headerText);
        // Deliberately quiet: a 1.15rem title over a 58px ball.
        overlayText.style.fontSize = isQuestion ? '1.6rem' : '1.15rem';
        tipsContainer.style.fontSize = '0.68rem';
        const ballPx = isQuestion ? '0px' : '58px';
        lottiePlayer.style.width = ballPx;
        lottiePlayer.style.height = ballPx;
        // The title shares its row with the animation and the pill's padding, so
        // it has meaningfully less room than when it was centered on its own.
        this._overlayTitleReserve = 150;
      } else {
        overlay.style.padding = '24px 0 0';
        overlay.style.gap = '0px';
        headerRow.style.maxWidth = 'none';
        headerRow.style.justifyContent = 'center';
        headerBox.style.flexDirection = 'column';
        headerBox.style.gap = '0px';
        headerBox.style.maxWidth = '92%';
        headerBox.style.padding = '12px 26px';
        textContainer.style.textAlign = 'center';
        tipsContainer.style.textAlign = 'center';
        // Extra breathing room below the title so the tip reads as its own thing,
        // not a subtitle.
        tipsContainer.style.padding = '18px 0 0';
        lottieContainer.style.width = '100%';
        // Big animation on its own line, directly under the pill.
        if (lottieContainer.parentNode !== overlay) overlay.insertBefore(lottieContainer, todoContainer);
        // Title is large while building, then shrinks once the todo list takes over.
        overlayText.style.fontSize = isPlan ? '1.8rem' : '2.5rem';
        // Tips track ~35% of the current title size.
        tipsContainer.style.fontSize = isPlan ? '0.63rem' : '0.875rem';
        const ballPx = isPlan ? '140px' : '240px';
        lottiePlayer.style.width = ballPx;
        lottiePlayer.style.height = ballPx;
        this._overlayTitleReserve = 70;
      }
    };

    // Toggle the overlay layouts:
    //   building → big centered animation + cycling tips, no box
    //   plan     → small animation up top + the cloned todo list box
    //   question → animation/tips stopped; the cloned question card takes the pane
    // Orthogonal to all three: whether a promo is on screen, which is what
    // applyChrome switches the status block's proportions on.
    const setOverlayMode = (mode) => {
      this._overlayMode = mode;
      const isPlan = mode === 'plan';
      const isQuestion = mode === 'question';
      // Building: title + tips + ball are centered as a group (animation doesn't
      // grow). Plan/question: top-aligned with the box filling the space below.
      overlay.style.justifyContent = (isPlan || isQuestion) ? 'flex-start' : 'center';
      lottieContainer.style.flex = '0 0 auto';
      // Stop the animation entirely while a question is up — it's the cue that Leo
      // has paused and needs an answer (rather than still working).
      lottieContainer.style.display = isQuestion ? 'none' : 'flex';
      // Tips keep cycling in building/plan, but are hidden while a question is up.
      tipsContainer.style.display = isQuestion ? 'none' : 'block';
      todoContainer.style.display = isPlan ? 'block' : 'none';
      questionContainer.style.display = isQuestion ? 'block' : 'none';
      // Whether a promo may appear in THIS layout is the mothership's call
      // (policy.modes), with one guarantee it can't override: never during a
      // question — Leo is blocked on the user, nothing competes with that.
      const withAd = adsVisible();
      adContainer.style.display = withAd ? 'block' : 'none';
      applyAdSizing();
      applyChrome(withAd, { isPlan, isQuestion });
      // Re-evaluate the "Your " drop since the title size just changed.
      this._fitOverlayTitle?.();
    };
    this._setOverlayMode = setOverlayMode;

    // The status block: animation on the left, title + tip stacked to its right,
    // the whole thing a compact pill pinned to the TOP-LEFT of the pane. The pill
    // background keeps the white text readable over the live site behind it.
    //
    // This used to be a centered column with a 240px animation under a 2.5rem
    // title, which is what the promo slot below now claims — see setOverlayMode.
    const headerText = document.createElement('div');
    headerText.style.display = 'flex';
    headerText.style.flexDirection = 'column';
    headerText.style.alignItems = 'flex-start';
    headerText.style.minWidth = '0';
    headerText.appendChild(textContainer);
    headerText.appendChild(tipsContainer);

    // Direction, gap, padding and max-width are chrome-dependent — applyChrome
    // owns them, along with where the animation lives.
    const headerBox = document.createElement('div');
    headerBox.style.flex = '0 0 auto';
    headerBox.style.display = 'flex';
    headerBox.style.alignItems = 'center';
    headerBox.style.boxSizing = 'border-box';
    headerBox.style.borderRadius = '14px';
    headerBox.style.background = 'rgba(0, 0, 0, 0.45)';
    headerBox.appendChild(headerText);

    headerRow.appendChild(headerBox);

    overlay.appendChild(headerRow);
    overlay.appendChild(todoContainer);
    overlay.appendChild(questionContainer);
    overlay.appendChild(adContainer);

    // Now that every node applyChrome touches exists, paint the initial layout.
    setOverlayMode('building');

    browserContent.appendChild(overlay);

    this.overlayElement = overlay;
    this._startOverlayAds(overlay, adContainer);

    // Keep the title on one line: drop the "Your " prefix when the pane is too
    // narrow to fit the full title, and restore it when there's room again. The
    // prefix span is looked up fresh each call since the title can change.
    const fitTitle = () => {
      const prefixSpan = overlayText.querySelector('.overlay-title-prefix');
      if (!prefixSpan) return;                          // current title has no "Your "
      prefixSpan.style.display = 'inline';              // try the full title first
      // Measure against the pane width (minus the pill's padding/margins), not
      // the now content-hugging title container. How much to reserve depends on
      // the chrome (compact shares the row with the animation), so applyChrome
      // sets it.
      const available = browserContent.clientWidth - (this._overlayTitleReserve || 70);
      if (overlayText.scrollWidth > available) {
        prefixSpan.style.display = 'none';              // too tight — drop "Your"
      }
    };
    this._fitOverlayTitle = fitTitle; // let setOverlayMode re-fit after size changes
    requestAnimationFrame(fitTitle); // measure once layout is settled
    const titleObserver = new ResizeObserver(fitTitle);
    titleObserver.observe(browserContent);
    this._overlayTitleObserver = titleObserver;

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
      // While a question is up, the overlay belongs to the question card — don't
      // let chat mutations flip the layout back to building/plan underneath it.
      if (this._overlayQuestionActive) return;
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

    // Exposed so clearQuestionFromOverlay() can recompute building-vs-plan once a
    // question is answered (revert to whatever the overlay was showing before).
    this._overlayDoMirror = doMirror;

    this._overlayObservers = [];
    const planObserver = new MutationObserver(schedule);
    planObserver.observe(history, { childList: true, subtree: true, characterData: true, attributes: true });
    this._overlayObservers.push(planObserver);

    // Mirror immediately in case a plan already exists when the overlay opens.
    doMirror();
  }

  /**
   * Fetch and mount the overlay's promo slot.
   *
   * Fire-and-forget by design: the overlay is already on screen and useful
   * without it, so nothing here is awaited and every failure path is "no slot".
   * `loadOverlayAds` never rejects; the only thing to guard is the race where
   * the user hides the overlay (or a new build starts) while the request is in
   * flight — hence the identity check against the overlay we were called for.
   */
  _startOverlayAds(overlay, slotEl) {
    loadOverlayAds().then(({ ads, rotateSeconds, policy, variant }) => {
      if (this.overlayElement !== overlay) return;   // overlay died mid-fetch

      this._overlayAdPolicy = policy;
      this._overlayAdVariant = variant;

      // WHEN a promo shows is the mothership's decision, not ours — see
      // evaluatePolicy. We only carry out the verdict and record why, so
      // leoAds.status() can answer "where's my ad?" without a code read.
      const verdict = evaluatePolicy({ ads, policy });
      this._overlayAdVerdict = verdict.reason;
      if (!verdict.show) return;

      const mount = () => {
        // The delay means the overlay may be long gone by the time we fire.
        if (this.overlayElement !== overlay) return;
        const rotator = new OverlayAdRotator({
          ads,
          rotateSeconds,
          // Promos can declare different heights; re-apply sizing whenever one
          // comes up (only actually changes anything in plan mode).
          onAdChange: () => this._applyOverlayAdSizing?.(),
        });
        if (!rotator.mount(slotEl)) return;
        this._overlayAdRotator = rotator;
        recordShown();                                // starts the cooldown clock
        // Re-run the current layout now that there IS a slot to show.
        this._setOverlayMode?.(this._overlayMode || 'building');
      };

      // Hold the promo back for the first few seconds so a short build doesn't
      // flash an ad and yank it away.
      if (verdict.delayMs > 0) {
        this._overlayAdDelayTimer = setTimeout(mount, verdict.delayMs);
      } else {
        mount();
      }
    });
  }

  /**
   * Tear the promo slot down (kills its rotation timer). Safe when none exists.
   */
  _stopOverlayAds() {
    if (this._overlayAdDelayTimer) {
      clearTimeout(this._overlayAdDelayTimer);
      this._overlayAdDelayTimer = null;
    }
    if (this._overlayAdRotator) {
      this._overlayAdRotator.stop();
      this._overlayAdRotator = null;
    }
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
   * Surface a question inside the building overlay: drop the prebuilt (and already
   * wired) clone into the overlay box, stop the animation, and switch the heading
   * to "Question from Leo" so the user knows Leo is waiting on them. The clone is
   * created/wired by MessageHandler; this only manages the overlay chrome.
   * No-op (returns false) when there's no overlay on screen.
   */
  showQuestionInOverlay(cloneEl) {
    const container = document.getElementById('overlayQuestionList');
    if (!this.overlayElement || !container || !cloneEl) return false;
    container.innerHTML = '';
    container.appendChild(cloneEl);
    this._overlayQuestionActive = true;
    // Leo can ask 1-4 questions on one card.
    const asked = Number(cloneEl.dataset?.count || 1);
    this._setOverlayTitle?.(asked > 1 ? `${asked} questions from Leo` : 'Question from Leo');
    this._setOverlayMode?.('question');
    return true;
  }

  /**
   * Tear down the in-overlay question and revert the overlay to whatever it was
   * showing before (building, or the mirrored todo list if a plan exists).
   * Safe to call unconditionally — no-ops when no question is active.
   */
  clearQuestionFromOverlay() {
    if (!this._overlayQuestionActive) return;
    this._overlayQuestionActive = false;
    const container = document.getElementById('overlayQuestionList');
    if (container) container.innerHTML = '';
    this._setOverlayTitle?.(this._overlayBaseTitle || 'Your App is Building!');
    // Recompute building-vs-plan from the current chat state.
    this._overlayDoMirror?.();
  }

  /**
   * Escape dismisses the "Your App is Building!" overlay.
   *
   * It HIDES, it does not cancel — same contract as the "Hide" pill in the
   * overlay's corner, which is why this routes through the identical teardown.
   * Leo keeps working and the run's own completion path is unaffected.
   *
   * Bound on `document` with no check on the event target: the whole point is
   * that the user can be typing in the composer, hit Escape, and see the
   * preview again. Guarding on focus would defeat that.
   *
   * The overlay-exists check keeps this inert the rest of the time, so Escape
   * still belongs to whatever else wants it (modals, the slash menu) whenever no
   * build is on screen.
   */
  initOverlayEscapeListener() {
    document.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      if (!document.getElementById('streamingOverlay')) return;
      this.removeStreamingOverlay();
    });
  }

  /**
   * Remove streaming overlay
   */
  removeStreamingOverlay() {
    this._stopOverlayPlanMirror();
    this._stopOverlayAds();
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
    this._overlayMode = null;
    this._applyOverlayAdSizing = null;
    this._overlayAdPolicy = null;
    this._overlayAdVariant = null;
    this._overlayAdVerdict = null;
    // Drop the question state too — the real card still lives in the chat; only the
    // throwaway overlay clone dies with the overlay, so nothing needs rescuing.
    this._overlayQuestionActive = false;
    this._overlayDoMirror = null;
    this._setOverlayTitle = null;
    this._overlayBaseTitle = null;
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
   * Refresh the Inbox iframe.
   *
   * Re-assigning .src rather than calling getInboxUrl() so a refresh keeps
   * whichever inbox page the user navigated to inside the frame, instead of
   * bouncing them back through the /inbox entry redirect.
   */
  refreshInboxFrame() {
    if (this.inboxFrame && this.inboxFrame.src) {
      this.inboxFrame.src = this.inboxFrame.src;
    }
  }

  /**
   * Refresh the Activity iframe
   */
  refreshActivityFrame() {
    if (this.activityFrame && this.activityFrame.src) {
      this.activityFrame.src = this.activityFrame.src;
    }
  }

  // ============================================================================
  // Unread messages badge
  // ============================================================================

  /**
   * Keep the red badge on the Messages tab in sync with the Rails app.
   *
   * The chat UI and the Rails app are different origins, so this window cannot
   * read the unread count itself — a fetch would need CORS plus cross-site
   * credentials. Instead the messages iframe (which already holds the Devise
   * session and an ActionCable subscription) posts the count up to us whenever
   * it changes, and we only render it.
   */
  initUnreadBadgeListener() {
    if (!this.messagesUnreadBadge) return;

    window.addEventListener('message', (event) => {
      const data = event.data;
      if (!data || data.source !== 'llamabot-notifications') return;
      if (data.type !== 'unread-count') return;

      // The frame is untrusted input like any other postMessage sender, so the
      // count is coerced and clamped rather than injected as-is.
      const count = Number(data.unreadMessages);
      this.setUnreadMessagesCount(Number.isFinite(count) ? Math.max(0, Math.floor(count)) : 0);
    });
  }

  /**
   * Render the unread count. Zero hides the badge entirely — an empty red dot
   * reads as "something is wrong" rather than "nothing to see".
   */
  setUnreadMessagesCount(count) {
    const badge = this.messagesUnreadBadge;
    if (!badge) return;

    if (count > 0) {
      badge.textContent = count > 99 ? '99+' : String(count);
      badge.setAttribute('aria-label', `${count} unread message${count === 1 ? '' : 's'}`);
      badge.classList.remove('hidden');
    } else {
      badge.textContent = '0';
      badge.removeAttribute('aria-label');
      badge.classList.add('hidden');
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
    this._rememberPath(path);

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
    const idToDataAttrMap = TAB_TARGET_TO_FRAME;

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

        // Remember the tab so a full page refresh comes back to it.
        this._writeStored('lastTab', targetIframeId);
      });
    });

    // Re-open whichever tab the user was last on.
    this._restoreActiveTab(tabs, iframes, idToDataAttrMap);

    // Initialize external link buttons
    this.initExternalLinkButtons();
  }

  /**
   * Restore the last-used tab on page load.
   *
   * Every iframe already has its src set by initIframeSources(), so this is a
   * pure CSS-class swap — no extra loading.
   */
  _restoreActiveTab(tabs, iframes, idToDataAttrMap) {
    const savedTarget = this._readStored('lastTab');
    if (!savedTarget) return;

    const tab = Array.from(tabs).find(t => t.dataset.target === savedTarget);
    if (!tab) return;

    // Never restore a tab this user can't see. The role gate that hides
    // engineer-only tabs runs later (on llamabot:ready), so checking the tab's
    // computed visibility here would always say "visible" — read the role
    // directly instead. Otherwise a 'user' would land on the Code tab with a
    // hidden, un-highlighted tab strip and no way back.
    const role = (typeof window !== 'undefined' && window.LLAMABOT_USER_ROLE) || 'engineer';
    if (role === 'user' && tab.dataset.engineerOnly === 'true') return;

    // Same reasoning for a tab switched off in Settings → Browser Tabs. The
    // chat.html gate also catches this, but it runs on llamabot:ready — doing
    // it here too means the hidden tab never flashes as active first.
    const visibleTabs = (typeof window !== 'undefined' && window.LLAMABOT_VISIBLE_TABS) || null;
    if (Array.isArray(visibleTabs) && savedTarget !== 'liveSiteFrame' && !visibleTabs.includes(savedTarget)) return;

    const dataAttrName = idToDataAttrMap[savedTarget] || savedTarget;
    const targetIframe = this.querySelector(`[data-llamabot="${dataAttrName}"]`);
    if (!targetIframe) return;

    tabs.forEach(t => t.classList.remove('active'));
    iframes.forEach(i => i.classList.remove('active'));
    tab.classList.add('active');
    targetIframe.classList.add('active');
  }

  /**
   * Initialize external link buttons on tabs
   */
  initExternalLinkButtons() {
    const externalLinkButtons = this.querySelectorAll('.tab-external-link');

    // Map old ID names to new data-llamabot attribute names
    const idToDataAttrMap = TAB_TARGET_TO_FRAME;

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
