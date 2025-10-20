/**
 * Auto-scroll behavior management
 */

import { CONFIG } from '../config.js';

export class ScrollManager {
  constructor(messageHistoryElement) {
    this.messageHistory = messageHistoryElement;
    this.isUserAtBottom = true;
    this.scrollThreshold = CONFIG.SCROLL_THRESHOLD;
    this.scrollButton = null;
    this.unreadCount = 0;
    this.unreadBadge = null;

    this.init();
  }

  /**
   * Initialize scroll manager
   */
  init() {
    this.scrollButton = document.getElementById('scrollToBottomBtn');
    console.log('ScrollManager: Button found:', !!this.scrollButton);

    // Create unread badge element
    if (this.scrollButton) {
      this.unreadBadge = document.createElement('span');
      this.unreadBadge.className = 'unread-badge';
      this.unreadBadge.style.display = 'none';
      this.scrollButton.appendChild(this.unreadBadge);
      console.log('ScrollManager: Unread badge created and attached');
    }

    // Add scroll event listener
    if (this.messageHistory) {
      this.messageHistory.addEventListener('scroll', () => {
        this.checkIfUserAtBottom();
      });
    }

    // Add button click listener
    if (this.scrollButton) {
      this.scrollButton.addEventListener('click', () => {
        this.scrollToBottomManually();
      });
    }
  }

  /**
   * Check if user is at bottom of message history
   */
  checkIfUserAtBottom() {
    if (!this.messageHistory) return false;

    const scrollTop = this.messageHistory.scrollTop;
    const scrollHeight = this.messageHistory.scrollHeight;
    const clientHeight = this.messageHistory.clientHeight;

    // Check if user is within threshold pixels of the bottom
    const wasAtBottom = this.isUserAtBottom;
    this.isUserAtBottom = (scrollTop + clientHeight >= scrollHeight - this.scrollThreshold);

    // Reset unread count if user scrolled to bottom
    if (!wasAtBottom && this.isUserAtBottom) {
      this.resetUnreadCount();
    }

    // Update button visibility
    this.updateScrollToBottomButton();

    return this.isUserAtBottom;
  }

  /**
   * Scroll to bottom of message history
   * @param {boolean} force - Force scroll even if user is not at bottom
   */
  scrollToBottom(force = false) {
    if (!this.messageHistory) return;

    // Only scroll if user is at bottom or force is true
    if (force || this.isUserAtBottom) {
      this.messageHistory.scrollTop = this.messageHistory.scrollHeight;
      this.isUserAtBottom = true;
      this.updateScrollToBottomButton();
    }
  }

  /**
   * Manually scroll to bottom (from button click)
   */
  scrollToBottomManually() {
    if (!this.messageHistory) return;

    this.messageHistory.scrollTop = this.messageHistory.scrollHeight;
    this.isUserAtBottom = true;
    this.resetUnreadCount();
    this.updateScrollToBottomButton();
  }

  /**
   * Update scroll-to-bottom button visibility
   */
  updateScrollToBottomButton() {
    if (!this.scrollButton) return;

    if (!this.isUserAtBottom) {
      this.scrollButton.classList.add('visible');
      console.log('ScrollManager: Button made visible');
    } else {
      this.scrollButton.classList.remove('visible');
      console.log('ScrollManager: Button hidden');
    }
  }

  /**
   * Get whether user is at bottom
   */
  getUserAtBottom() {
    return this.isUserAtBottom;
  }

  /**
   * Increment unread message count
   */
  incrementUnreadCount() {
    if (!this.isUserAtBottom) {
      this.unreadCount++;
      console.log('ScrollManager: Unread count incremented to', this.unreadCount);
      this.updateUnreadBadge();
    }
  }

  /**
   * Reset unread message count
   */
  resetUnreadCount() {
    this.unreadCount = 0;
    this.updateUnreadBadge();
  }

  /**
   * Update unread badge display
   */
  updateUnreadBadge() {
    if (!this.unreadBadge) {
      console.log('ScrollManager: No unread badge element!');
      return;
    }

    if (this.unreadCount > 0) {
      this.unreadBadge.textContent = this.unreadCount > 99 ? '99+' : this.unreadCount.toString();
      this.unreadBadge.style.display = 'flex';
      console.log('ScrollManager: Badge showing count:', this.unreadBadge.textContent);
    } else {
      this.unreadBadge.style.display = 'none';
      console.log('ScrollManager: Badge hidden');
    }
  }
}
