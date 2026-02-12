/**
 * Panel Resize Manager
 * Handles resizing of the chat panel and collapsing to sidebar mode
 */

export class PanelResizeManager {
  constructor(container = null, elements = {}, callbacks = {}) {
    this.container = container || document;
    this.elements = elements;
    this.callbacks = callbacks;

    // Configuration
    this.MIN_WIDTH = 280;
    this.COLLAPSE_THRESHOLD = 150;
    this.COLLAPSED_WIDTH = 64;
    this.MAX_WIDTH_PERCENT = 0.6;
    this.STORAGE_KEY = 'llamabot-chat-panel-width';

    // State
    this.isDragging = false;
    this.isCollapsed = false;
    this.currentWidth = null;
    this.previousWidth = null; // Store width before collapse for restore

    // Element references
    this.chatSection = null;
    this.iframeSection = null;
    this.resizeHandle = null;
    this.collapseBtn = null;

    this.init();
  }

  /**
   * Helper method for scoped queries
   */
  querySelector(selector) {
    return this.container.querySelector(selector);
  }

  /**
   * Initialize the panel resize manager
   */
  init() {
    this.cacheElements();
    this.loadSavedWidth();
    this.bindEvents();
    this.bindCollapsedToolbarEvents();
  }

  /**
   * Cache DOM element references
   */
  cacheElements() {
    this.chatSection = this.querySelector('.chat-section');
    this.iframeSection = this.querySelector('.iframe-section');
    this.resizeHandle = this.querySelector('[data-llamabot="resize-handle"]');
    this.collapseBtn = this.querySelector('[data-llamabot="collapse-sidebar-btn"]');
  }

  /**
   * Load saved width from localStorage
   */
  loadSavedWidth() {
    const saved = localStorage.getItem(this.STORAGE_KEY);
    if (saved) {
      const data = JSON.parse(saved);
      if (data.collapsed) {
        this.previousWidth = data.previousWidth || this.getDefaultWidth();
        this.collapse();
      } else if (data.width) {
        this.setWidth(data.width);
        this.previousWidth = data.width;
      }
    }
  }

  /**
   * Save current width to localStorage
   */
  saveWidth() {
    const data = {
      collapsed: this.isCollapsed,
      width: this.currentWidth,
      previousWidth: this.previousWidth
    };
    localStorage.setItem(this.STORAGE_KEY, JSON.stringify(data));
  }

  /**
   * Get default width (33.33% of viewport)
   */
  getDefaultWidth() {
    return Math.round(window.innerWidth * 0.3333);
  }

  /**
   * Bind event listeners
   */
  bindEvents() {
    if (!this.resizeHandle || !this.collapseBtn) return;

    // Resize handle events
    this.resizeHandle.addEventListener('mousedown', this.onDragStart.bind(this));
    this.resizeHandle.addEventListener('touchstart', this.onDragStart.bind(this), { passive: false });

    // Double-click to toggle collapse
    this.resizeHandle.addEventListener('dblclick', this.toggleCollapse.bind(this));

    // Collapse button
    this.collapseBtn.addEventListener('click', this.toggleCollapse.bind(this));

    // Global mouse/touch events for dragging
    document.addEventListener('mousemove', this.onDragMove.bind(this));
    document.addEventListener('mouseup', this.onDragEnd.bind(this));
    document.addEventListener('touchmove', this.onDragMove.bind(this), { passive: false });
    document.addEventListener('touchend', this.onDragEnd.bind(this));

    // Window resize
    window.addEventListener('resize', this.onWindowResize.bind(this));
  }

  /**
   * Bind events for collapsed toolbar buttons to trigger their expanded counterparts
   */
  bindCollapsedToolbarEvents() {
    // Expand button - expands the sidebar
    const expandBtn = this.querySelector('[data-llamabot="collapsed-expand-btn"]');
    if (expandBtn) {
      expandBtn.addEventListener('click', () => {
        this.expand();
      });
    }

    // Map collapsed toolbar buttons to their expanded counterparts
    const buttonMappings = [
      { collapsed: 'collapsed-screen-record-btn', expanded: 'screen-record-btn' },
      { collapsed: 'collapsed-screenshot-btn', expanded: 'screenshot-btn' }
    ];

    buttonMappings.forEach(({ collapsed, expanded }) => {
      const collapsedBtn = this.querySelector(`[data-llamabot="${collapsed}"]`);
      const expandedBtn = this.querySelector(`[data-llamabot="${expanded}"]`);

      if (collapsedBtn && expandedBtn) {
        collapsedBtn.addEventListener('click', () => {
          expandedBtn.click();
        });
      }
    });
  }

  /**
   * Handle drag start
   */
  onDragStart(e) {
    if (this.isMobile()) return;

    e.preventDefault();
    this.isDragging = true;
    this.resizeHandle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    // Disable transitions during drag for smooth resizing
    this.chatSection.style.transition = 'none';
    this.iframeSection.style.transition = 'none';
  }

  /**
   * Handle drag move
   */
  onDragMove(e) {
    if (!this.isDragging) return;

    e.preventDefault();

    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const maxWidth = window.innerWidth * this.MAX_WIDTH_PERCENT;

    let newWidth = clientX;

    // Check if we should collapse
    if (newWidth < this.COLLAPSE_THRESHOLD) {
      // Visual feedback that we're in collapse zone
      this.resizeHandle.classList.add('collapse-zone');
    } else {
      this.resizeHandle.classList.remove('collapse-zone');

      // Clamp width
      newWidth = Math.max(this.MIN_WIDTH, Math.min(maxWidth, newWidth));
      this.setWidth(newWidth);
    }
  }

  /**
   * Handle drag end
   */
  onDragEnd(e) {
    if (!this.isDragging) return;

    this.isDragging = false;
    this.resizeHandle.classList.remove('dragging', 'collapse-zone');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';

    // Re-enable transitions
    this.chatSection.style.transition = '';
    this.iframeSection.style.transition = '';

    // Check if we should collapse
    const clientX = e.changedTouches ? e.changedTouches[0].clientX : e.clientX;
    if (clientX < this.COLLAPSE_THRESHOLD) {
      this.collapse();
    } else {
      this.saveWidth();
    }
  }

  /**
   * Set the chat panel width
   */
  setWidth(width) {
    if (!this.chatSection || !this.iframeSection) return;

    this.currentWidth = width;

    // Remove collapsed state if setting a width
    if (this.isCollapsed && width > this.COLLAPSED_WIDTH) {
      this.isCollapsed = false;
      this.chatSection.classList.remove('collapsed');
    }

    this.chatSection.style.width = `${width}px`;
    this.iframeSection.style.width = `calc(100% - ${width}px - 6px)`; // 6px for resize handle
  }

  /**
   * Collapse the sidebar
   */
  collapse() {
    if (!this.chatSection) return;

    // Store current width before collapsing (if not already collapsed)
    if (!this.isCollapsed && this.currentWidth > this.COLLAPSED_WIDTH) {
      this.previousWidth = this.currentWidth;
    }

    this.isCollapsed = true;
    this.chatSection.classList.add('collapsed');
    this.chatSection.style.width = '';
    this.iframeSection.style.width = `calc(100% - ${this.COLLAPSED_WIDTH}px - 6px)`;

    this.saveWidth();
  }

  /**
   * Expand the sidebar
   */
  expand() {
    if (!this.chatSection) return;

    this.isCollapsed = false;
    this.chatSection.classList.remove('collapsed');

    // Restore previous width or use default
    const width = this.previousWidth || this.getDefaultWidth();
    this.setWidth(width);

    this.saveWidth();
  }

  /**
   * Toggle collapse state
   */
  toggleCollapse() {
    if (this.isCollapsed) {
      this.expand();
    } else {
      this.collapse();
    }
  }

  /**
   * Handle window resize
   */
  onWindowResize() {
    // On mobile, reset to default
    if (this.isMobile()) {
      this.chatSection.style.width = '';
      this.iframeSection.style.width = '';
      return;
    }

    // Ensure width doesn't exceed max
    if (this.currentWidth && !this.isCollapsed) {
      const maxWidth = window.innerWidth * this.MAX_WIDTH_PERCENT;
      if (this.currentWidth > maxWidth) {
        this.setWidth(maxWidth);
      }
    }
  }

  /**
   * Check if we're in mobile view
   */
  isMobile() {
    return window.innerWidth <= 768;
  }

  /**
   * Get current collapsed state
   */
  getIsCollapsed() {
    return this.isCollapsed;
  }

  /**
   * Get current width
   */
  getCurrentWidth() {
    return this.currentWidth;
  }
}
