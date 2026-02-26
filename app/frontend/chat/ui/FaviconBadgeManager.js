/**
 * Favicon badge management for error/completion notifications
 * Shows a colored badge overlay on the browser tab favicon
 */

export class FaviconBadgeManager {
  constructor() {
    this.faviconLink = document.querySelector('link[rel="icon"]');
    this.originalFaviconUrl = this.faviconLink?.href || '';
    this.originalFaviconImage = null;
    this.canvas = null;
    this.ctx = null;
    this.isReady = false;

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
      this.clear();
    });

    // Also clear on visibility change (tab becomes visible)
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) {
        this.clear();
      }
    });
  }

  /**
   * Load the original favicon image for canvas drawing
   */
  loadOriginalFavicon() {
    this.originalFaviconImage = new Image();
    this.originalFaviconImage.crossOrigin = 'anonymous';

    this.originalFaviconImage.onload = () => {
      this.isReady = true;
    };

    this.originalFaviconImage.onerror = () => {
      // If CORS fails, create a simple fallback icon
      console.warn('FaviconBadgeManager: Could not load favicon with CORS, using fallback');
      this.createFallbackIcon();
    };

    this.originalFaviconImage.src = this.originalFaviconUrl;
  }

  /**
   * Create a simple fallback icon if CORS prevents loading the original
   */
  createFallbackIcon() {
    // Create a simple gradient circle as fallback
    const fallbackCanvas = document.createElement('canvas');
    fallbackCanvas.width = 32;
    fallbackCanvas.height = 32;
    const fallbackCtx = fallbackCanvas.getContext('2d');

    // Draw a simple llama-themed icon (purple gradient circle)
    const gradient = fallbackCtx.createRadialGradient(16, 16, 0, 16, 16, 16);
    gradient.addColorStop(0, '#8b5cf6');
    gradient.addColorStop(1, '#6d28d9');

    fallbackCtx.beginPath();
    fallbackCtx.arc(16, 16, 14, 0, 2 * Math.PI);
    fallbackCtx.fillStyle = gradient;
    fallbackCtx.fill();

    // Use this as our "original" image
    this.originalFaviconImage = new Image();
    this.originalFaviconImage.src = fallbackCanvas.toDataURL('image/png');
    this.originalFaviconImage.onload = () => {
      this.isReady = true;
    };
  }

  /**
   * Draw the favicon with a badge overlay
   * @param {string} badgeColor - The color of the badge (e.g., '#ef4444' for red)
   */
  drawBadge(badgeColor) {
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
   * Show error badge (red)
   */
  showError() {
    // Only show badge if tab is not focused
    if (document.hasFocus()) return;

    this.drawBadge('#ef4444'); // Tailwind red-500
  }

  /**
   * Show completion badge (green)
   */
  showComplete() {
    // Only show badge if tab is not focused
    if (document.hasFocus()) return;

    this.drawBadge('#22c55e'); // Tailwind green-500
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
