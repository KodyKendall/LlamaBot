/**
 * Mobile view and responsive behavior management
 */

export class MobileViewManager {
  constructor(scrollManager) {
    this.scrollManager = scrollManager;
    this.currentMobileView = 'chat'; // Default view
    this.init();
  }

  /**
   * Initialize mobile view manager
   */
  init() {
    this.initializeMobileView();
    this.initResizeListener();
    this.initTextareaAutoResize();
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
    const messageInput = document.getElementById('messageInput');
    if (messageInput) {
      messageInput.addEventListener('input', function() {
        this.style.height = 'auto';
        this.style.height = this.scrollHeight + 'px';
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

// Make switchToMobileView available globally for onclick handlers
window.switchToMobileView = function(view) {
  if (window.mobileViewManager) {
    window.mobileViewManager.switchToMobileView(view);
  }
};
