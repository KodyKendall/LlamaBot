/**
 * Mobile view and responsive behavior management
 */

export class MobileViewManager {
  constructor(scrollManager, container = null, elements = {}) {
    this.scrollManager = scrollManager;
    this.container = container || document;
    this.elements = elements;
    this.currentMobileView = 'chat'; // Default view
    this.init();
  }

  /**
   * Helper method for scoped queries
   */
  querySelector(selector) {
    return this.container.querySelector(selector);
  }

  /**
   * Initialize mobile view manager
   */
  init() {
    this.initializeMobileView();
    this.initResizeListener();
    this.initTextareaAutoResize();
    this.initMobileViewButtons();
  }

  /**
   * Initialize mobile view on page load
   */
  initializeMobileView() {
    if (window.innerWidth <= 768) {
      document.body.classList.add('mobile-chat-view');
      this.currentMobileView = 'chat';
    }
  }

  /**
   * Switch mobile view between chat and iframe
   */
  switchToMobileView(view) {
    const body = document.body;

    // Remove existing classes
    body.classList.remove('mobile-chat-view', 'mobile-iframe-view');

    // Add appropriate class
    if (view === 'chat') {
      body.classList.add('mobile-chat-view');
      this.currentMobileView = 'chat';

      // Scroll to bottom when switching to chat
      setTimeout(() => {
        if (this.scrollManager) {
          this.scrollManager.checkIfUserAtBottom();
          if (!this.scrollManager.getUserAtBottom()) {
            this.scrollManager.scrollToBottom(true);
          }
        }
      }, 300);
    } else if (view === 'iframe') {
      body.classList.add('mobile-iframe-view');
      this.currentMobileView = 'iframe';
    }
  }

  /**
   * Handle window resize
   */
  handleResize() {
    if (window.innerWidth <= 768) {
      // Mobile view - ensure we have a mobile view class
      if (!document.body.classList.contains('mobile-chat-view') &&
          !document.body.classList.contains('mobile-iframe-view')) {
        this.switchToMobileView(this.currentMobileView);
      }
    } else {
      // Desktop view - remove mobile classes
      document.body.classList.remove('mobile-chat-view', 'mobile-iframe-view');
    }
  }

  /**
   * Initialize resize listener
   */
  initResizeListener() {
    window.addEventListener('resize', () => {
      this.handleResize();
    });
  }

  /**
   * Initialize textarea auto-resize
   */
  initTextareaAutoResize() {
    const messageInput = this.elements.messageInput || this.querySelector('[data-llamabot="message-input"]');
    if (messageInput) {
      messageInput.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = this.scrollHeight + 'px';
      });
    }
  }

  /**
   * Initialize mobile view toggle buttons
   */
  initMobileViewButtons() {
    // Button to show preview (switch to iframe view)
    const showPreviewBtn = this.querySelector('[data-llamabot="show-preview-btn"]');
    if (showPreviewBtn) {
      showPreviewBtn.addEventListener('click', () => {
        this.switchToMobileView('iframe');
      });
    }

    // Button to show chat (switch back to chat view)
    const showChatBtn = this.querySelector('[data-llamabot="show-chat-btn"]');
    if (showChatBtn) {
      showChatBtn.addEventListener('click', () => {
        this.switchToMobileView('chat');
      });
    }
  }

  /**
   * Get current mobile view
   */
  getCurrentView() {
    return this.currentMobileView;
  }
}
