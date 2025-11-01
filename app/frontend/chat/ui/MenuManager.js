/**
 * Hamburger menu and drawer management
 */

export class MenuManager {
  constructor() {
    this.hamburger = null;
    this.drawer = null;
    this.newThreadBtn = null;
    this.init();
  }

  /**
   * Initialize menu manager
   */
  init() {
    this.hamburger = document.getElementById('hamburgerMenu');
    this.drawer = document.getElementById('menuDrawer');
    this.newThreadBtn = document.getElementById('newThreadBtn');

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
    const closeDrawer = document.getElementById('closeDrawer');
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
