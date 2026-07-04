/**
 * PromptManager
 *
 * Manages the prompt library panel in the chat interface.
 * Allows users to browse, search, select, and quick-edit prompts to attach to messages.
 *
 * NOTE: This panel used to have a second "Skills" tab (DB-backed prompt-blobs
 * multi-selected and concatenated into every message). Skills are now Agent
 * Skills on the filesystem (.leonardo/skills/<slug>/SKILL.md, the SKILL.md open
 * standard) that the model loads on demand via the use_skill tool — so the
 * legacy Skills tab was removed and this manager is prompts-only.
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
    // Edit modal
    this.editModal = null;
    this.editingItem = null;
  }

  /**
   * Initialize the prompt manager
   */
  init(button, messageInput, inputArea) {
    this.button = button;
    this.messageInput = messageInput;
    this.inputArea = inputArea;

    if (!this.button) {
      console.warn('Prompt library button not found');
      return;
    }

    // Create the panel and edit modal
    this.createPanel();
    this.createEditModal();

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
          !this.button.contains(e.target) &&
          (!this.editModal || !this.editModal.contains(e.target))) {
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
        <div class="prompt-panel-tabs">
          <span class="prompt-panel-title">Prompts</span>
        </div>
        <button class="prompt-panel-add" title="Add new prompt">
          <i class="fa-solid fa-plus"></i>
        </button>
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

    // Add button
    this.panel.querySelector('.prompt-panel-add').addEventListener('click', (e) => {
      e.stopPropagation();
      this.showCreateModal();
    });

    // Close button
    this.panel.querySelector('.prompt-panel-close').addEventListener('click', () => {
      this.closePanel();
    });

    // Search input
    const searchInput = this.panel.querySelector('.prompt-search-input');
    let searchTimeout;
    searchInput.addEventListener('input', () => {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(() => this.loadPrompts(), 300);
    });

    // Group filter
    this.panel.querySelector('.prompt-group-select').addEventListener('change', () => {
      this.loadPrompts();
    });

    // Prevent panel clicks from propagating
    this.panel.addEventListener('click', (e) => {
      e.stopPropagation();
    });
  }

  /**
   * Create the edit/create modal
   */
  createEditModal() {
    this.editModal = document.createElement('div');
    this.editModal.className = 'prompt-edit-modal';
    this.editModal.innerHTML = `
      <div class="prompt-edit-modal-content">
        <div class="prompt-edit-modal-header">
          <h3 class="prompt-edit-modal-title">New Prompt</h3>
          <button class="prompt-edit-modal-close">&times;</button>
        </div>
        <form class="prompt-edit-form">
          <div class="prompt-edit-field">
            <label>Name *</label>
            <input type="text" name="name" required placeholder="e.g., Code Review Instructions">
          </div>
          <div class="prompt-edit-field">
            <label>Group</label>
            <input type="text" name="group" value="General" placeholder="e.g., Engineering">
          </div>
          <div class="prompt-edit-field">
            <label>Description (optional)</label>
            <input type="text" name="description" placeholder="Brief description">
          </div>
          <div class="prompt-edit-field">
            <label>Content *</label>
            <textarea name="content" required placeholder="Enter content..."></textarea>
          </div>
          <div class="prompt-edit-actions">
            <button type="button" class="prompt-edit-delete" style="display: none;">
              <i class="fa-solid fa-trash"></i> Delete
            </button>
            <div class="prompt-edit-actions-right">
              <button type="button" class="prompt-edit-cancel">Cancel</button>
              <button type="submit" class="prompt-edit-save">Save</button>
            </div>
          </div>
        </form>
      </div>
    `;

    document.body.appendChild(this.editModal);

    // Close button
    this.editModal.querySelector('.prompt-edit-modal-close').addEventListener('click', () => {
      this.closeEditModal();
    });

    // Cancel button
    this.editModal.querySelector('.prompt-edit-cancel').addEventListener('click', () => {
      this.closeEditModal();
    });

    // Delete button
    this.editModal.querySelector('.prompt-edit-delete').addEventListener('click', () => {
      this.deleteEditingItem();
    });

    // Form submit
    this.editModal.querySelector('.prompt-edit-form').addEventListener('submit', (e) => {
      e.preventDefault();
      this.saveEditingItem();
    });

    // Close on outside click
    this.editModal.addEventListener('click', (e) => {
      if (e.target === this.editModal) {
        this.closeEditModal();
      }
    });

    // Prevent clicks inside modal from closing panel
    this.editModal.querySelector('.prompt-edit-modal-content').addEventListener('click', (e) => {
      e.stopPropagation();
    });
  }

  /**
   * Show modal for creating a new prompt
   */
  showCreateModal() {
    this.editingItem = null;

    this.editModal.querySelector('.prompt-edit-modal-title').textContent = 'New Prompt';
    this.editModal.querySelector('.prompt-edit-delete').style.display = 'none';

    // Clear form
    const form = this.editModal.querySelector('.prompt-edit-form');
    form.name.value = '';
    form.group.value = 'General';
    form.description.value = '';
    form.content.value = '';

    this.editModal.querySelector('.prompt-edit-save').className = 'prompt-edit-save';
    this.editModal.classList.add('open');
  }

  /**
   * Show modal for editing an existing prompt
   */
  showEditModal(item) {
    this.editingItem = item;

    this.editModal.querySelector('.prompt-edit-modal-title').textContent = 'Edit Prompt';
    this.editModal.querySelector('.prompt-edit-delete').style.display = 'block';

    // Fill form
    const form = this.editModal.querySelector('.prompt-edit-form');
    form.name.value = item.name;
    form.group.value = item.group;
    form.description.value = item.description || '';
    form.content.value = item.content;

    this.editModal.querySelector('.prompt-edit-save').className = 'prompt-edit-save';
    this.editModal.classList.add('open');
  }

  /**
   * Close the edit modal
   */
  closeEditModal() {
    this.editModal.classList.remove('open');
    this.editingItem = null;
  }

  /**
   * Save the prompt being edited/created
   */
  async saveEditingItem() {
    const form = this.editModal.querySelector('.prompt-edit-form');
    const data = {
      name: form.name.value.trim(),
      group: form.group.value.trim() || 'General',
      description: form.description.value.trim() || null,
      content: form.content.value
    };

    if (!data.name || !data.content) {
      alert('Name and content are required');
      return;
    }

    const method = this.editingItem ? 'PATCH' : 'POST';
    const url = this.editingItem ? `/api/prompts/${this.editingItem.id}` : '/api/prompts';

    try {
      const response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });

      if (response.ok) {
        this.closeEditModal();
        await this.loadGroups();
        await this.loadPrompts();
      } else {
        const error = await response.json();
        alert(error.detail || 'Error saving');
      }
    } catch (error) {
      console.error('Error saving:', error);
      alert('Error saving: ' + error.message);
    }
  }

  /**
   * Delete the prompt being edited
   */
  async deleteEditingItem() {
    if (!this.editingItem) return;
    if (!confirm('Delete this prompt?')) return;

    try {
      const response = await fetch(`/api/prompts/${this.editingItem.id}`, { method: 'DELETE' });
      if (response.ok) {
        this.closeEditModal();
        await this.loadGroups();
        await this.loadPrompts();
      } else {
        const error = await response.json();
        alert(error.detail || 'Error deleting');
      }
    } catch (error) {
      console.error('Error deleting:', error);
      alert('Error deleting: ' + error.message);
    }
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
   * Open the panel and load data
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
          <button class="prompt-create-btn" onclick="this.closest('.prompt-library-panel').querySelector('.prompt-panel-add').click()">
            <i class="fa-solid fa-plus"></i> Create your first prompt
          </button>
        </div>
      `;
      return;
    }

    list.innerHTML = this.prompts.map(p => `
      <div class="prompt-item" data-id="${p.id}">
        <div class="prompt-item-header">
          <span class="prompt-item-name">${this.escapeHtml(p.name)}</span>
          <button class="prompt-item-edit" data-id="${p.id}" title="Edit">
            <i class="fa-solid fa-pen"></i>
          </button>
          <span class="prompt-item-group">${this.escapeHtml(p.group)}</span>
        </div>
        ${p.description ? '<div class="prompt-item-description">' + this.escapeHtml(p.description) + '</div>' : ''}
        <div class="prompt-item-preview">${this.escapeHtml(p.content.substring(0, 100))}${p.content.length > 100 ? '...' : ''}</div>
      </div>
    `).join('');

    // Add click handlers for selection
    list.querySelectorAll('.prompt-item').forEach(item => {
      item.addEventListener('click', (e) => {
        // Don't select if clicking edit button
        if (e.target.closest('.prompt-item-edit')) return;
        const id = parseInt(item.dataset.id);
        this.selectPrompt(id);
      });
    });

    // Add click handlers for edit buttons
    list.querySelectorAll('.prompt-item-edit').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = parseInt(btn.dataset.id);
        const prompt = this.prompts.find(p => p.id === id);
        if (prompt) this.showEditModal(prompt);
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
      <span class="badge-text" title="Click to view full prompt">${this.escapeHtml(prompt.name)}</span>
      <div class="badge-tooltip">${this.escapeHtml(prompt.content)}</div>
      <button class="badge-close" title="Remove prompt">&times;</button>
    `;

    // Expanded popup element
    const expandedPopup = document.createElement('div');
    expandedPopup.className = 'prompt-expanded-popup';
    expandedPopup.innerHTML = `
      <div class="prompt-expanded-header">
        <span class="prompt-expanded-title">${this.escapeHtml(prompt.name)}</span>
        <button class="prompt-expanded-close" title="Close">&times;</button>
      </div>
      <div class="prompt-expanded-content">${this.escapeHtml(prompt.content)}</div>
    `;
    badge.appendChild(expandedPopup);

    // Close expanded popup
    expandedPopup.querySelector('.prompt-expanded-close').addEventListener('click', (e) => {
      e.stopPropagation();
      expandedPopup.classList.remove('open');
    });

    // Click on badge text to toggle expanded popup
    badge.querySelector('.badge-text').addEventListener('click', (e) => {
      e.stopPropagation();
      expandedPopup.classList.toggle('open');
    });

    // Click on badge icon also toggles expanded popup
    badge.querySelector('.badge-icon').addEventListener('click', (e) => {
      e.stopPropagation();
      expandedPopup.classList.toggle('open');
    });

    // Close popup when clicking outside
    document.addEventListener('click', (e) => {
      if (expandedPopup.classList.contains('open') &&
          !expandedPopup.contains(e.target) &&
          !badge.querySelector('.badge-text').contains(e.target) &&
          !badge.querySelector('.badge-icon').contains(e.target)) {
        expandedPopup.classList.remove('open');
      }
    });

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
   * Clear all selections after message is sent
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
    if (this.editModal) {
      this.editModal.remove();
    }
  }
}
