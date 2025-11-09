/**
 * Hamburger menu and drawer management
 */

export class MenuManager {
  constructor(container = null) {
    this.container = container || document;
    this.hamburger = null;
    this.drawer = null;
    this.newThreadBtn = null;
    this.init();
  }

  /**
   * Helper method for scoped queries
   */
  querySelector(selector) {
    return this.container.querySelector(selector);
  }

  /**
   * Initialize menu manager
   */
  init() {
    this.hamburger = this.querySelector('[data-llamabot="hamburger-menu"]');
    this.drawer = this.querySelector('[data-llamabot="menu-drawer"]');
    this.newThreadBtn = this.querySelector('[data-llamabot="new-thread-btn"]');

    this.initEventListeners();
  }

  /**
   * Initialize event listeners
   */
  initEventListeners() {
    // Hamburger menu click
    if (this.hamburger) {
      this.hamburger.addEventListener('click', (e) => {
        e.stopPropagation();
        this.toggleMenu();
      });
    }

    // New thread button click
    if (this.newThreadBtn) {
      this.newThreadBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        // Dispatch event that ThreadManager can listen to
        window.dispatchEvent(new CustomEvent('createNewThread'));
      });
    }

    // Close drawer button
    const closeDrawer = this.querySelector('[data-llamabot="close-drawer"]');
    if (closeDrawer) {
      closeDrawer.addEventListener('click', () => {
        this.closeMenu();
      });
    }

    // Close menu when clicking outside
    document.addEventListener('click', (event) => {
      if (!this.hamburger || !this.drawer) return;

      if (!this.hamburger.contains(event.target) && !this.drawer.contains(event.target)) {
        this.closeMenu();
      }
    });
  }

  /**
   * Toggle menu open/closed
   */
  toggleMenu() {
    if (!this.hamburger || !this.drawer) return;

    this.hamburger.classList.toggle('active');
    this.drawer.classList.toggle('open');
  }

  /**
   * Close menu
   */
  closeMenu() {
    if (!this.hamburger || !this.drawer) return;

    this.hamburger.classList.remove('active');
    this.drawer.classList.remove('open');
  }

  /**
   * Open menu
   */
  openMenu() {
    if (!this.hamburger || !this.drawer) return;

    this.hamburger.classList.add('active');
    this.drawer.classList.add('open');
  }

  /**
   * Check if menu is open
   */
  isOpen() {
    return this.drawer?.classList.contains('open') || false;
  }
}
