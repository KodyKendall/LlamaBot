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
    // Multiple selections: each entry is { text, html }, in the order picked.
    this.selectedElements = [];
    this.badgeContainer = null;

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

    // Re-arm the iframe every time it finishes loading.
    //
    // The enable is a one-shot postMessage, and the iframe reloads constantly —
    // after every agent edit and every navigation from the chat header. Two
    // everyday sequences used to leave the button lit with a dead tool behind
    // it: clicking during a load (the Rails page's message listener isn't
    // registered yet, so the message is dropped), and a reload while the tool is
    // on (the fresh document starts with selection mode off). Both looked to the
    // user like "the tool doesn't work" — the next click read as "turn off", so
    // it took three clicks to recover.
    //
    // `load` is observable cross-origin from the parent and fires after the
    // document's module scripts have run, so the listener is there by then.
    const frame = this.iframeManager && this.iframeManager.liveSiteFrame;
    if (frame) {
      frame.addEventListener('load', () => {
        if (this.isSelectionMode) this._postEnable();
      });
    }
  }

  /**
   * Tell the iframe to enter selection mode.
   *
   * A frame mid-navigation can have a null contentWindow; a re-arm must never
   * take the chat UI down with it.
   */
  _postEnable() {
    const frame = this.iframeManager && this.iframeManager.liveSiteFrame;
    if (!frame || !frame.contentWindow) return;

    frame.contentWindow.postMessage({
      source: 'leonardo',
      type: 'enable-element-selector'
    }, '*');
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
    this.selectorButton.dataset.tooltip = 'Selection mode on — click an element, or click here to turn it off';
    this.selectorButton.setAttribute('aria-label', 'Disable selection mode');

    // Mirror disableSelectionMode()'s defensive write. toggleSelectionMode() has
    // already set this today, but the re-arm on iframe load reads the flag as the
    // source of truth — leaving it to the one caller that happens to set it makes
    // a direct enableSelectionMode() call silently un-re-armable.
    this.isSelectionMode = true;

    // Send message to iframe to enable selection mode
    this._postEnable();
  }

  /**
   * Disable selection mode
   */
  disableSelectionMode() {
    // Update button appearance
    this.selectorButton.classList.remove('active');
    this.selectorButton.dataset.tooltip = 'Click an element on the page to point Leo at what to change';
    this.selectorButton.setAttribute('aria-label', 'Select element from page');

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

    // Append this selection to the list (don't replace prior ones), so the
    // user can re-open selection mode and pick a 2nd, 3rd, ... element.
    this.selectedElements.push({ text: textContent, html: htmlContent });

    // Re-render the badges (labelled 1st, 2nd, ...)
    this.renderBadges();

    // Focus the message input
    this.messageInput.focus();

    // Trigger input event to update UI (e.g., enable send button)
    this.messageInput.dispatchEvent(new Event('input', { bubbles: true }));

    // Disable selection mode after selection
    this.disableSelectionMode();
  }

  /**
   * Convert a 1-based index to an ordinal label (1st, 2nd, 3rd, 4th, ...)
   */
  ordinalLabel(n) {
    const s = ['th', 'st', 'nd', 'rd'];
    const v = n % 100;
    return n + (s[(v - 20) % 10] || s[v] || s[0]);
  }

  /**
   * Render the badges for every selected element.
   */
  renderBadges() {
    // Ensure a container exists directly before the message input.
    if (!this.badgeContainer || !this.badgeContainer.isConnected) {
      this.badgeContainer = document.createElement('div');
      this.badgeContainer.className = 'selected-elements-container';
      this.messageInput.parentElement.insertBefore(this.badgeContainer, this.messageInput);
    }

    // Rebuild badges from the current selection list.
    this.badgeContainer.innerHTML = '';

    if (this.selectedElements.length === 0) {
      this.badgeContainer.remove();
      this.badgeContainer = null;
      return;
    }

    this.selectedElements.forEach((element, index) => {
      const label = this.ordinalLabel(index + 1);

      const badge = document.createElement('div');
      badge.className = 'selected-element-badge';

      const icon = document.createElement('span');
      icon.className = 'badge-icon';
      icon.textContent = '🎯';

      const text = document.createElement('span');
      text.className = 'badge-text';
      // e.g. "1st: Save button"
      text.textContent = `${label}: ${element.text}`;
      text.title = element.text;

      const closeBtn = document.createElement('button');
      closeBtn.className = 'badge-close';
      closeBtn.title = 'Remove selection';
      closeBtn.textContent = '×';
      closeBtn.addEventListener('click', () => {
        this.removeSelectionAt(index);
      });

      badge.appendChild(icon);
      badge.appendChild(text);
      badge.appendChild(closeBtn);
      this.badgeContainer.appendChild(badge);
    });
  }

  /**
   * Remove a single selection by index and re-render.
   */
  removeSelectionAt(index) {
    this.selectedElements.splice(index, 1);
    this.renderBadges();
  }

  /**
   * Get the ordered list of selected elements ({ text, html }).
   */
  getSelectedElements() {
    return this.selectedElements;
  }

  /**
   * Get the selected element HTML (if any) to append to message.
   * Returns the combined HTML of all selections for backwards compatibility.
   */
  getSelectedElementHTML() {
    if (this.selectedElements.length === 0) return null;
    return this.selectedElements.map((el) => el.html).join('\n\n');
  }

  /**
   * Clear all selected elements after message is sent.
   */
  clearSelection() {
    this.selectedElements = [];
    this.renderBadges();
  }

  /**
   * Cleanup on destroy
   */
  destroy() {
    window.removeEventListener('message', this.handlePostMessage);
    this.clearSelection();
  }
}
