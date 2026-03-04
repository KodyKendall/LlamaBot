/**
 * Favicon badge management for notifications
 * Shows colored badge overlays on the browser tab favicon:
 * - Red badge: error occurred
 * - Green badge: task completed
 * - Pulsing indicator: Leonardo is thinking/working
 * - Number badge: unread message count
 */

export class FaviconBadgeManager {
  constructor() {
    this.faviconLink = document.querySelector('link[rel="icon"]');
    this.originalFaviconUrl = '/frontend/leonardo-icon.png'; // Use local copy to avoid CORS
    this.originalFaviconImage = null;
    this.canvas = null;
    this.ctx = null;
    this.isReady = false;
    this.thinkingAnimationId = null;
    this.thinkingFrame = 0;
    this.currentState = 'idle'; // 'idle', 'thinking', 'error', 'complete', 'unread'
    this.unreadCount = 0;

    // Pre-generated thinking frames (data URLs) for background tab animation
    this.thinkingFrameOn = null;
    this.thinkingFrameOff = null;

    this.init();
  }

  /**
   * Initialize the favicon badge manager
   */
  init() {
    if (!this.faviconLink) {
      console.warn('FaviconBadgeManager: No favicon link found');
      return;
    }

    // Create canvas for drawing
    this.canvas = document.createElement('canvas');
    this.canvas.width = 32;
    this.canvas.height = 32;
    this.ctx = this.canvas.getContext('2d');

    // Preload the original favicon image
    this.loadOriginalFavicon();

    // Clear badge when window gains focus
    window.addEventListener('focus', () => {
      this.clearNotification();
    });

    // Also clear on visibility change (tab becomes visible)
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        this.clearNotification();
      }
    });
  }

  /**
   * Load the original favicon image for canvas drawing
   */
  loadOriginalFavicon() {
    this.originalFaviconImage = new Image();

    this.originalFaviconImage.onload = () => {
      this.isReady = true;
      // Pre-generate thinking animation frames once image is loaded
      this.preGenerateThinkingFrames();
    };

    this.originalFaviconImage.onerror = () => {
      console.warn('FaviconBadgeManager: Could not load local favicon');
      // Try the S3 URL as fallback
      this.originalFaviconImage.src = 'https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/4bmqe5iolvp84ceyk9ttz8vylrym';
    };

    this.originalFaviconImage.src = this.originalFaviconUrl;
  }

  /**
   * Draw the favicon with a colored dot badge overlay
   * @param {string} badgeColor - The color of the badge
   */
  drawDotBadge(badgeColor) {
    if (!this.isReady || !this.ctx || !this.originalFaviconImage) {
      return;
    }

    // Clear canvas
    this.ctx.clearRect(0, 0, 32, 32);

    // Draw original favicon
    this.ctx.drawImage(this.originalFaviconImage, 0, 0, 32, 32);

    // Draw badge circle (bottom-right corner)
    const badgeRadius = 8;
    const badgeCenterX = 24;
    const badgeCenterY = 24;

    // White border/outline
    this.ctx.beginPath();
    this.ctx.arc(badgeCenterX, badgeCenterY, badgeRadius + 1, 0, 2 * Math.PI);
    this.ctx.fillStyle = 'white';
    this.ctx.fill();

    // Colored badge
    this.ctx.beginPath();
    this.ctx.arc(badgeCenterX, badgeCenterY, badgeRadius, 0, 2 * Math.PI);
    this.ctx.fillStyle = badgeColor;
    this.ctx.fill();

    // Update favicon
    this.updateFavicon(this.canvas.toDataURL('image/png'));
  }

  /**
   * Draw the favicon with a number badge (for unread count)
   * @param {number} count - The number to display
   * @param {string} bgColor - Background color of the badge
   */
  drawCountBadge(count, bgColor = '#ef4444') {
    if (!this.isReady || !this.ctx || !this.originalFaviconImage) {
      return;
    }

    // Clear canvas
    this.ctx.clearRect(0, 0, 32, 32);

    // Draw original favicon
    this.ctx.drawImage(this.originalFaviconImage, 0, 0, 32, 32);

    // Badge dimensions
    const badgeHeight = 14;
    const badgeMinWidth = 14;
    const badgePadding = 3;
    const displayText = count > 99 ? '99+' : String(count);

    // Measure text width
    this.ctx.font = 'bold 10px Arial';
    const textWidth = this.ctx.measureText(displayText).width;
    const badgeWidth = Math.max(badgeMinWidth, textWidth + badgePadding * 2);

    // Position (top-right corner)
    const badgeX = 32 - badgeWidth - 1;
    const badgeY = 1;

    // White border
    this.ctx.beginPath();
    this.ctx.roundRect(badgeX - 1, badgeY - 1, badgeWidth + 2, badgeHeight + 2, 4);
    this.ctx.fillStyle = 'white';
    this.ctx.fill();

    // Colored background
    this.ctx.beginPath();
    this.ctx.roundRect(badgeX, badgeY, badgeWidth, badgeHeight, 3);
    this.ctx.fillStyle = bgColor;
    this.ctx.fill();

    // Text
    this.ctx.fillStyle = 'white';
    this.ctx.textAlign = 'center';
    this.ctx.textBaseline = 'middle';
    this.ctx.fillText(displayText, badgeX + badgeWidth / 2, badgeY + badgeHeight / 2 + 1);

    // Update favicon
    this.updateFavicon(this.canvas.toDataURL('image/png'));
  }

  /**
   * Pre-generate thinking animation frames as data URLs
   * This allows us to just swap URLs in background tabs without canvas operations
   */
  preGenerateThinkingFrames() {
    if (!this.ctx || !this.originalFaviconImage) return;

    // Generate "on" frame (bright, larger)
    this.ctx.clearRect(0, 0, 32, 32);
    this.ctx.drawImage(this.originalFaviconImage, 0, 0, 32, 32);
    this.drawPurpleDot(8, 1.0);
    this.thinkingFrameOn = this.canvas.toDataURL('image/png');

    // Generate "off" frame (dimmer, smaller)
    this.ctx.clearRect(0, 0, 32, 32);
    this.ctx.drawImage(this.originalFaviconImage, 0, 0, 32, 32);
    this.drawPurpleDot(5, 0.4);
    this.thinkingFrameOff = this.canvas.toDataURL('image/png');
  }

  /**
   * Helper to draw a purple dot badge
   */
  drawPurpleDot(radius, opacity) {
    const badgeCenterX = 24;
    const badgeCenterY = 24;

    // White border
    this.ctx.beginPath();
    this.ctx.arc(badgeCenterX, badgeCenterY, radius + 1.5, 0, 2 * Math.PI);
    this.ctx.fillStyle = 'white';
    this.ctx.fill();

    // Purple dot
    this.ctx.beginPath();
    this.ctx.arc(badgeCenterX, badgeCenterY, radius, 0, 2 * Math.PI);
    this.ctx.fillStyle = `rgba(139, 92, 246, ${opacity})`;
    this.ctx.fill();
  }

  /**
   * Draw thinking/working animation frame
   * Uses pre-generated frames for reliable background tab animation
   */
  drawThinkingFrame() {
    if (!this.thinkingFrameOn || !this.thinkingFrameOff) {
      return;
    }

    // Alternate between two pre-generated states (blink)
    const isOn = this.thinkingFrame % 2 === 0;
    const frameUrl = isOn ? this.thinkingFrameOn : this.thinkingFrameOff;

    // Just swap the URL - no canvas operations needed
    this.updateFavicon(frameUrl);
  }

  /**
   * Update the favicon link with a new image
   * @param {string} dataUrl - The data URL of the new favicon
   */
  updateFavicon(dataUrl) {
    if (!this.faviconLink) return;

    // Remove old favicon and create new one to force browser refresh
    const newLink = document.createElement('link');
    newLink.rel = 'icon';
    newLink.type = 'image/png';
    newLink.href = dataUrl;

    // Replace the old link
    this.faviconLink.parentNode.replaceChild(newLink, this.faviconLink);
    this.faviconLink = newLink;
  }

  /**
   * Start thinking animation
   */
  startThinking() {
    if (this.currentState === 'thinking') return;

    this.currentState = 'thinking';
    this.thinkingFrame = 0;

    // Draw initial frame immediately
    this.drawThinkingFrame();

    // Use setInterval with 1000ms because browsers throttle background tabs
    // to a minimum of 1 second. This creates a blink effect.
    this.thinkingAnimationId = setInterval(() => {
      if (this.currentState !== 'thinking') {
        this.stopThinking();
        return;
      }

      this.thinkingFrame++;
      this.drawThinkingFrame();
    }, 1000);
  }

  /**
   * Stop thinking animation
   */
  stopThinking() {
    if (this.thinkingAnimationId) {
      clearInterval(this.thinkingAnimationId);
      this.thinkingAnimationId = null;
    }

    if (this.currentState === 'thinking') {
      this.currentState = 'idle';
      this.clear();
    }
  }

  /**
   * Show error badge (red dot)
   */
  showError() {
    this.stopThinking();

    // Only show badge if tab is not focused
    if (document.hasFocus()) return;

    this.currentState = 'error';
    this.drawDotBadge('#ef4444'); // Tailwind red-500
  }

  /**
   * Show completion badge (green dot)
   */
  showComplete() {
    this.stopThinking();

    // Only show badge if tab is not focused
    if (document.hasFocus()) return;

    this.currentState = 'complete';
    this.drawDotBadge('#22c55e'); // Tailwind green-500
  }

  /**
   * Update unread count badge
   * @param {number} count - Number of unread messages
   */
  updateUnreadCount(count) {
    this.unreadCount = count;

    // Only show if tab not focused and count > 0
    if (document.hasFocus() || count === 0) {
      if (this.currentState === 'unread') {
        this.clear();
      }
      return;
    }

    // Don't override error/complete states
    if (this.currentState === 'error' || this.currentState === 'complete') {
      return;
    }

    this.currentState = 'unread';
    this.drawCountBadge(count, '#ef4444'); // Red background
  }

  /**
   * Clear notification badges (error/complete/unread) but respect thinking state
   */
  clearNotification() {
    if (this.currentState === 'thinking') {
      // Keep thinking animation going
      return;
    }

    this.currentState = 'idle';
    this.unreadCount = 0;
    this.clear();
  }

  /**
   * Clear the badge and restore original favicon
   */
  clear() {
    if (!this.faviconLink) return;

    // Restore original favicon
    this.updateFavicon(this.originalFaviconUrl);
  }
}
