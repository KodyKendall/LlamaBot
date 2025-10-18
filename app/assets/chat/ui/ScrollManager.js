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

    this.init();
  }

  /**
   * Initialize scroll manager
   */
  init() {
    this.scrollButton = document.getElementById('scrollToBottomBtn');

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
    this.isUserAtBottom = (scrollTop + clientHeight >= scrollHeight - this.scrollThreshold);

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
    this.updateScrollToBottomButton();
  }

  /**
   * Update scroll-to-bottom button visibility
   */
  updateScrollToBottomButton() {
    if (!this.scrollButton) return;

    if (!this.isUserAtBottom) {
      this.scrollButton.classList.add('visible');
    } else {
      this.scrollButton.classList.remove('visible');
    }
  }

  /**
   * Get whether user is at bottom
   */
  getUserAtBottom() {
    return this.isUserAtBottom;
  }
}
