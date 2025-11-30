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

import { getRailsUrl, getVSCodeUrl, DEFAULT_CONFIG } from '../config.js';

export class IframeManager {
  constructor(container = null) {
    this.container = container || document;

    // STREAMING PREVIEW iframe (for HTML generation preview)
    this.contentFrame = this.querySelector('[data-llamabot="content-frame"]');

    // RAILS APP PREVIEW iframe (for live Rails app)
    this.liveSiteFrame = this.querySelector('[data-llamabot="live-site-frame"]');

    // VS CODE iframe
    this.vsCodeFrame = this.querySelector('[data-llamabot="vscode-frame"]');

    // URL input element
    this.urlInput = this.querySelector('[data-llamabot="url-input"]');

    // URL dropdown element
    this.urlDropdown = this.querySelector('[data-llamabot="url-dropdown"]');

    // Cached routes
    this.cachedRoutes = null;

    this.overlayElement = null;

    // Navigation history stack for back button (since we can't access cross-origin iframe history)
    this.navigationHistory = [];

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
            console.log('Navigation tracked:', fromPath, '->', toPath, 'History:', this.navigationHistory);
          }
        }

        // Update URL bar immediately when navigation starts
        if (this.urlInput && toPath) {
          this.urlInput.value = toPath;
        }
      } else if (event.data.type === 'page-loaded') {
        // Update URL display when Rails app loads a new page
        if (this.urlInput && event.data.path) {
          this.urlInput.value = event.data.path;
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
      console.log('Error updating streaming preview iframe (normal during streaming):', e);
    }
  }

  /**
   * Create streaming overlay with animation
   * Used during HTML generation to show progress animation
   */
  createStreamingOverlay() {
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
    overlay.style.zIndex = '10';
    overlay.style.borderRadius = '8px';

    // Create text
    const overlayText = document.createElement('div');
    overlayText.textContent = 'Your Page is Being Built!';
    overlayText.style.color = 'white';
    overlayText.style.fontSize = '2.5rem';
    overlayText.style.fontWeight = 'bold';
    overlayText.style.fontFamily = 'Arial, sans-serif';
    overlayText.style.textShadow = '2px 2px 4px rgba(0,0,0,0.5)';

    const textContainer = document.createElement('div');
    textContainer.style.padding = '30px';
    textContainer.style.width = '100%';
    textContainer.style.textAlign = 'center';
    textContainer.appendChild(overlayText);

    // Create Lottie container
    const lottieContainer = document.createElement('div');
    lottieContainer.id = 'lottieAnimation';
    lottieContainer.style.width = '300px';
    lottieContainer.style.height = '300px';
    lottieContainer.style.position = 'absolute';
    lottieContainer.style.top = '50%';
    lottieContainer.style.left = '50%';
    lottieContainer.style.transform = 'translate(-50%, -50%)';

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
    lottiePlayer.style.width = "300px";
    lottiePlayer.style.height = "300px";
    lottiePlayer.setAttribute("autoplay", "");
    lottiePlayer.setAttribute("loop", "");

    lottieContainer.appendChild(lottiePlayer);
    overlay.appendChild(textContainer);
    overlay.appendChild(lottieContainer);
    browserContent.appendChild(overlay);

    this.overlayElement = overlay;
  }

  /**
   * Remove streaming overlay
   */
  removeStreamingOverlay() {
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
        console.log('debugInfoJson', debugInfoJson);

        if (this.liveSiteFrame.src) {
          let additionalRequestPath = debugInfoJson.request_path;

          if (!additionalRequestPath) {
            console.warn('Warning: debugInfoJson.request_path is undefined! Rails error likely.', debugInfoJson);
            additionalRequestPath = '/';
          }

          this.liveSiteFrame.src = getRailsUrl() + additionalRequestPath;
        }
      });
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
      console.log('Could not update URL display:', e);
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
