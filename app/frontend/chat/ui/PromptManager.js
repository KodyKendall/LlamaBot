/**
 * PromptManager
 *
 * Manages the prompt library panel in the chat interface.
 * Allows users to browse, search, and attach prompts to messages.
 * Follows the pattern established by ElementSelector.js
 */

export class PromptManager {
  constructor() {
    this.isOpen = false;
    this.prompts = [];
    this.groups = [];
    this.selectedPrompt = null;
    this.selectedBadge = null;
    this.panel = null;
    this.button = null;
    this.messageInput = null;
    this.inputArea = null;
  }

  /**
   * Initialize the prompt manager
   * @param {HTMLElement} button - The prompt library toggle button
   * @param {HTMLElement} messageInput - The message input textarea
   * @param {HTMLElement} inputArea - The input area container for panel placement
   */
  init(button, messageInput, inputArea) {
    this.button = button;
    this.messageInput = messageInput;
    this.inputArea = inputArea;

    if (!this.button) {
      console.warn('Prompt library button not found');
      return;
    }

    // Create the panel
    this.createPanel();

    // Add click handler to toggle panel
    this.button.addEventListener('click', (e) => {
      e.stopPropagation();
      this.togglePanel();
    });

    // Close panel when clicking outside
    document.addEventListener('click', (e) => {
      if (this.isOpen &&
          this.panel &&
          !this.panel.contains(e.target) &&
          !this.button.contains(e.target)) {
        this.closePanel();
      }
    });
  }

  /**
   * Create the prompt library panel HTML
   */
  createPanel() {
    this.panel = document.createElement('div');
    this.panel.className = 'prompt-library-panel';
    this.panel.innerHTML = `
      <div class="prompt-panel-header">
        <span class="prompt-panel-title">Prompt Library</span>
        <a href="/prompt-library" class="prompt-panel-manage" title="Manage prompts">
          <i class="fa-solid fa-gear"></i>
        </a>
        <button class="prompt-panel-close" title="Close">&times;</button>
      </div>
      <div class="prompt-panel-search">
        <input type="text" placeholder="Search prompts..." class="prompt-search-input">
        <select class="prompt-group-select">
          <option value="">All Groups</option>
        </select>
      </div>
      <div class="prompt-panel-list">
        <div class="prompt-empty">Loading...</div>
      </div>
    `;

    // Insert panel before the thinking area or at the start of input area
    const thinkingArea = this.inputArea.querySelector('[data-llamabot="thinking-area"]');
    if (thinkingArea) {
      this.inputArea.insertBefore(this.panel, thinkingArea);
    } else {
      this.inputArea.insertBefore(this.panel, this.inputArea.firstChild);
    }

    // Event listeners
    this.panel.querySelector('.prompt-panel-close').addEventListener('click', () => {
      this.closePanel();
    });

    const searchInput = this.panel.querySelector('.prompt-search-input');
    let searchTimeout;
    searchInput.addEventListener('input', () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => this.loadPrompts(), 300);
    });

    this.panel.querySelector('.prompt-group-select').addEventListener('change', () => {
      this.loadPrompts();
    });

    // Prevent panel clicks from propagating
    this.panel.addEventListener('click', (e) => {
      e.stopPropagation();
    });
  }

  /**
   * Toggle panel visibility
   */
  togglePanel() {
    if (this.isOpen) {
      this.closePanel();
    } else {
      this.openPanel();
    }
  }

  /**
   * Open the panel and load prompts
   */
  async openPanel() {
    this.isOpen = true;
    this.panel.classList.add('open');
    this.button.classList.add('active');

    await this.loadGroups();
    await this.loadPrompts();

    // Focus search input
    const searchInput = this.panel.querySelector('.prompt-search-input');
    if (searchInput) {
      searchInput.focus();
    }
  }

  /**
   * Close the panel
   */
  closePanel() {
    this.isOpen = false;
    this.panel.classList.remove('open');
    this.button.classList.remove('active');
  }

  /**
   * Load prompt groups from API
   */
  async loadGroups() {
    try {
      const response = await fetch('/api/prompts/groups');
      const data = await response.json();
      this.groups = data.groups;

      const select = this.panel.querySelector('.prompt-group-select');
      select.innerHTML = '<option value="">All Groups</option>';
      this.groups.forEach(g => {
        const option = document.createElement('option');
        option.value = g;
        option.textContent = g;
        select.appendChild(option);
      });
    } catch (error) {
      console.error('Failed to load prompt groups:', error);
    }
  }

  /**
   * Load prompts from API
   */
  async loadPrompts() {
    try {
      const search = this.panel.querySelector('.prompt-search-input').value;
      const group = this.panel.querySelector('.prompt-group-select').value;

      let url = '/api/prompts';
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      if (group) params.set('group', group);
      if (params.toString()) url += '?' + params.toString();

      const response = await fetch(url);
      this.prompts = await response.json();
      this.renderPrompts();
    } catch (error) {
      console.error('Failed to load prompts:', error);
      this.panel.querySelector('.prompt-panel-list').innerHTML =
        '<div class="prompt-error">Failed to load prompts</div>';
    }
  }

  /**
   * Render prompts in the panel
   */
  renderPrompts() {
    const list = this.panel.querySelector('.prompt-panel-list');

    if (this.prompts.length === 0) {
      list.innerHTML = `
        <div class="prompt-empty">
          <p>No prompts found</p>
          <a href="/prompt-library" class="prompt-create-link">Create your first prompt</a>
        </div>
      `;
      return;
    }

    list.innerHTML = this.prompts.map(p => `
      <div class="prompt-item" data-id="${p.id}">
        <div class="prompt-item-header">
          <span class="prompt-item-name">${this.escapeHtml(p.name)}</span>
          <span class="prompt-item-group">${this.escapeHtml(p.group)}</span>
        </div>
        ${p.description ? '<div class="prompt-item-description">' + this.escapeHtml(p.description) + '</div>' : ''}
        <div class="prompt-item-preview">${this.escapeHtml(p.content.substring(0, 100))}${p.content.length > 100 ? '...' : ''}</div>
      </div>
    `).join('');

    // Add click handlers
    list.querySelectorAll('.prompt-item').forEach(item => {
      item.addEventListener('click', () => {
        const id = parseInt(item.dataset.id);
        this.selectPrompt(id);
      });
    });
  }

  /**
   * Select a prompt to attach to the message
   */
  async selectPrompt(id) {
    const prompt = this.prompts.find(p => p.id === id);
    if (!prompt) return;

    this.selectedPrompt = prompt;

    // Show badge
    this.showSelectedBadge(prompt);

    // Track usage
    try {
      await fetch(`/api/prompts/${id}/use`, { method: 'POST' });
    } catch (error) {
      console.error('Failed to track prompt usage:', error);
    }

    // Close panel
    this.closePanel();

    // Focus message input
    if (this.messageInput) {
      this.messageInput.focus();
    }
  }

  /**
   * Show badge indicating selected prompt
   */
  showSelectedBadge(prompt) {
    this.removeSelectedBadge();

    const badge = document.createElement('div');
    badge.className = 'prompt-selected-badge';
    badge.innerHTML = `
      <span class="badge-icon"><i class="fa-solid fa-book"></i></span>
      <span class="badge-text">${this.escapeHtml(prompt.name)}</span>
      <div class="badge-tooltip">${this.escapeHtml(prompt.content)}</div>
      <button class="badge-close" title="Remove prompt">&times;</button>
    `;

    badge.querySelector('.badge-close').addEventListener('click', (e) => {
      e.stopPropagation();
      this.removeSelectedBadge();
    });

    // Insert badge before textarea
    if (this.messageInput && this.messageInput.parentElement) {
      this.messageInput.parentElement.insertBefore(badge, this.messageInput);
    }
    this.selectedBadge = badge;
  }

  /**
   * Remove selected prompt badge
   */
  removeSelectedBadge() {
    if (this.selectedBadge) {
      this.selectedBadge.remove();
      this.selectedBadge = null;
      this.selectedPrompt = null;
    }
  }

  /**
   * Get the selected prompt content to prepend to message
   */
  getSelectedPromptContent() {
    if (this.selectedPrompt) {
      return this.selectedPrompt.content;
    }
    return null;
  }

  /**
   * Clear selection after message is sent
   */
  clearSelection() {
    this.removeSelectedBadge();
  }

  /**
   * Escape HTML to prevent XSS
   */
  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Cleanup
   */
  destroy() {
    this.removeSelectedBadge();
    if (this.panel) {
      this.panel.remove();
    }
  }
}
