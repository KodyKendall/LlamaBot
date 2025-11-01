/**
 * Element Selector
 *
 * Enables selection mode for clicking elements within the Rails iframe
 * to copy their text content into the message input field.
 * Uses postMessage API for cross-origin iframe communication.
 */

export class ElementSelector {
  constructor(iframeManager) {
    this.iframeManager = iframeManager;
    this.isSelectionMode = false;
    this.selectorButton = null;
    this.messageInput = null;
    this.selectedElementHTML = null;
    this.selectedBadge = null;

    // Bind methods
    this.handlePostMessage = this.handlePostMessage.bind(this);
  }

  /**
   * Initialize the element selector
   * @param {HTMLElement} button - The selector toggle button
   * @param {HTMLElement} messageInput - The message input textarea
   */
  init(button, messageInput) {
    this.selectorButton = button;
    this.messageInput = messageInput;

    if (!this.selectorButton) {
      console.warn('Element selector button not found');
      return;
    }

    // Add click handler to toggle selection mode
    this.selectorButton.addEventListener('click', () => {
      this.toggleSelectionMode();
    });

    // Listen for messages from the iframe
    window.addEventListener('message', this.handlePostMessage);
  }

  /**
   * Handle messages from the iframe
   */
  handlePostMessage(event) {
    // Check if message is from our element selector
    if (event.data && event.data.source === 'element-selector') {
      if (event.data.type === 'element-selected') {
        this.handleElementSelected(event.data.text, event.data.html);
      }
    }
  }

  /**
   * Toggle selection mode on/off
   */
  toggleSelectionMode() {
    this.isSelectionMode = !this.isSelectionMode;

    if (this.isSelectionMode) {
      this.enableSelectionMode();
    } else {
      this.disableSelectionMode();
    }
  }

  /**
   * Enable selection mode
   */
  enableSelectionMode() {
    if (!this.iframeManager.liveSiteFrame) {
      console.warn('Rails iframe not found');
      return;
    }

    // Update button appearance
    this.selectorButton.classList.add('active');
    this.selectorButton.title = 'Selection mode active - Click to disable';

    // Send message to iframe to enable selection mode
    this.iframeManager.liveSiteFrame.contentWindow.postMessage({
      source: 'leonardo',
      type: 'enable-element-selector'
    }, '*');
  }

  /**
   * Disable selection mode
   */
  disableSelectionMode() {
    // Update button appearance
    this.selectorButton.classList.remove('active');
    this.selectorButton.title = 'Select element from page';

    if (this.iframeManager.liveSiteFrame) {
      // Send message to iframe to disable selection mode
      this.iframeManager.liveSiteFrame.contentWindow.postMessage({
        source: 'leonardo',
        type: 'disable-element-selector'
      }, '*');
    }

    this.isSelectionMode = false;
  }

  /**
   * Handle element selected from iframe
   */
  handleElementSelected(textContent, htmlContent) {
    if (!textContent || !this.messageInput) return;

    // Store the HTML content for later use
    this.selectedElementHTML = htmlContent;

    // Create or update the selected element badge
    this.showSelectedBadge(textContent);

    // Focus the message input
    this.messageInput.focus();

    // Trigger input event to update UI (e.g., enable send button)
    this.messageInput.dispatchEvent(new Event('input', { bubbles: true }));

    // Disable selection mode after selection
    this.disableSelectionMode();
  }

  /**
   * Show a badge indicating the selected element
   */
  showSelectedBadge(textContent) {
    // Remove existing badge if any
    this.removeSelectedBadge();

    // Create badge element
    const badge = document.createElement('div');
    badge.className = 'selected-element-badge';
    badge.innerHTML = `
      <span class="badge-icon">🎯</span>
      <span class="badge-text">Selected: ${textContent}</span>
      <button class="badge-close" title="Remove selection">×</button>
    `;

    // Add close button handler
    const closeBtn = badge.querySelector('.badge-close');
    closeBtn.addEventListener('click', () => {
      this.removeSelectedBadge();
    });

    // Insert badge before the message input
    this.messageInput.parentElement.insertBefore(badge, this.messageInput);
    this.selectedBadge = badge;
  }

  /**
   * Remove the selected element badge
   */
  removeSelectedBadge() {
    if (this.selectedBadge) {
      this.selectedBadge.remove();
      this.selectedBadge = null;
      this.selectedElementHTML = null;
    }
  }

  /**
   * Get the selected element HTML (if any) to append to message
   */
  getSelectedElementHTML() {
    return this.selectedElementHTML;
  }

  /**
   * Clear the selected element after message is sent
   */
  clearSelection() {
    this.removeSelectedBadge();
  }

  /**
   * Cleanup on destroy
   */
  destroy() {
    window.removeEventListener('message', this.handlePostMessage);
    this.removeSelectedBadge();
  }
}
