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

import { getRailsUrl, CONFIG } from '../config.js';

export class IframeManager {
  constructor() {
    // STREAMING PREVIEW iframe (for HTML generation preview)
    this.contentFrame = document.getElementById('contentFrame');

    // RAILS APP PREVIEW iframe (for live Rails app)
    this.liveSiteFrame = document.getElementById('liveSiteFrame');

    this.overlayElement = null;
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
   *
   * @param {Function} getRailsDebugInfoCallback - Function that accepts a callback parameter
   *                                                The callback will receive debugInfoJson
   *
   * Example usage:
   *   iframeManager.refreshRailsApp((callback) => this.getRailsDebugInfo(callback))
   */
  refreshRailsApp(getRailsDebugInfoCallback) {
    const iframes = document.querySelectorAll('iframe');

    iframes.forEach(iframe => {
      const isRailsIFrame = iframe.src.includes(':3000') || iframe.src.includes('https://rails-');

      if (isRailsIFrame) {
        getRailsDebugInfoCallback((debugInfoJson) => {
          console.log('debugInfoJson', debugInfoJson);

          if (iframe.src) {
            let additionalRequestPath = debugInfoJson.request_path;

            if (!additionalRequestPath) {
              console.warn('Warning: debugInfoJson.request_path is undefined! Rails error likely.', debugInfoJson);
              additionalRequestPath = '/';
            }

            iframe.src = getRailsUrl() + additionalRequestPath;
          }
        });
      } else {
        // Refresh non-Rails iframes by reloading their current src
        if (iframe.src) {
          iframe.src = iframe.src;
        }
      }
    });
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

  // ============================================================================
  // UI Controls (for both iframe types)
  // ============================================================================

  /**
   * Initialize navigation buttons (refresh, back, etc.)
   */
  initNavigationButtons() {
    // Refresh button
    const refreshButton = document.getElementById('refreshButton');
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

    // Back button
    const backButton = document.getElementById('backButton');
    if (backButton && this.liveSiteFrame) {
      backButton.addEventListener('click', () => {
        if (this.liveSiteFrame.contentWindow) {
          try {
            this.liveSiteFrame.contentWindow.history.back();
          } catch (error) {
            console.log('Cannot access iframe history due to cross-origin restrictions');

            // Provide visual feedback
            backButton.style.transform = 'scale(0.9)';
            setTimeout(() => {
              backButton.style.transform = '';
            }, 150);
          }
        }
      });
    }
  }

  /**
   * Init tab switching
   */
  initTabSwitching() {
    const tabs = document.querySelectorAll('.tab');
    const iframes = document.querySelectorAll('.content-iframe');

    tabs.forEach(tab => {
      tab.addEventListener('click', () => {
        tabs.forEach(t => t.classList.remove('active'));
        iframes.forEach(i => i.classList.remove('active'));

        tab.classList.add('active');
        const targetIframeId = tab.dataset.target;
        const targetIframe = document.getElementById(targetIframeId);
        if (targetIframe) {
          targetIframe.classList.add('active');
        }
      });
    });
  }

  /**
   * Init view mode toggle
   */
  initViewModeToggle() {
    const desktopModeBtn = document.getElementById('desktopModeBtn');
    const mobileModeBtn = document.getElementById('mobileModeBtn');
    const browserContent = document.querySelector('.browser-content');

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
