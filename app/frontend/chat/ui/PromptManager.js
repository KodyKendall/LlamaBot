/**
 * PromptManager
 *
 * Manages the prompt library panel in the chat interface.
 * Allows users to browse, search, and attach prompts to messages.
 * Also manages skills - stackable prompt snippets that can be multi-selected.
 * Includes quick add/edit functionality without leaving the chat interface.
 */

export class PromptManager {
  constructor() {
    this.isOpen = false;
    this.activeTab = 'prompts'; // 'prompts' or 'skills'
    this.prompts = [];
    this.groups = [];
    this.selectedPrompt = null;
    this.selectedBadge = null;
    // Skills support
    this.skills = [];
    this.skillGroups = [];
    this.selectedSkills = []; // Array of selected skill objects (multi-select)
    this.skillsBadgeContainer = null;
    this.panel = null;
    this.button = null;
    this.messageInput = null;
    this.inputArea = null;
    // Edit modal
    this.editModal = null;
    this.editingItem = null;
    this.editingType = null; // 'prompt' or 'skill'
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
          <button class="prompt-panel-tab active" data-tab="prompts">Prompts</button>
          <button class="prompt-panel-tab" data-tab="skills">Skills</button>
        </div>
        <button class="prompt-panel-add" title="Add new">
          <i class="fa-solid fa-plus"></i>
        </button>
        <button class="prompt-panel-close" title="Close">&times;</button>
      </div>
      <div class="prompt-panel-search">
        <input type="text" placeholder="Search..." class="prompt-search-input">
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

    // Tab switching
    this.panel.querySelectorAll('.prompt-panel-tab').forEach(tab => {
      tab.addEventListener('click', (e) => {
        e.stopPropagation();
        const tabName = tab.dataset.tab;
        this.switchTab(tabName);
      });
    });

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
      searchTimeout = setTimeout(() => this.loadCurrentTabData(), 300);
    });

    // Group filter
    this.panel.querySelector('.prompt-group-select').addEventListener('change', () => {
      this.loadCurrentTabData();
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
   * Show modal for creating new prompt/skill
   */
  showCreateModal() {
    this.editingItem = null;
    this.editingType = this.activeTab === 'prompts' ? 'prompt' : 'skill';

    const title = this.editingType === 'prompt' ? 'New Prompt' : 'New Skill';
    this.editModal.querySelector('.prompt-edit-modal-title').textContent = title;
    this.editModal.querySelector('.prompt-edit-delete').style.display = 'none';

    // Clear form
    const form = this.editModal.querySelector('.prompt-edit-form');
    form.name.value = '';
    form.group.value = 'General';
    form.description.value = '';
    form.content.value = '';

    // Update save button color
    const saveBtn = this.editModal.querySelector('.prompt-edit-save');
    saveBtn.className = 'prompt-edit-save' + (this.editingType === 'skill' ? ' skill' : '');

    this.editModal.classList.add('open');
  }

  /**
   * Show modal for editing existing prompt/skill
   */
  showEditModal(item, type) {
    this.editingItem = item;
    this.editingType = type;

    const title = type === 'prompt' ? 'Edit Prompt' : 'Edit Skill';
    this.editModal.querySelector('.prompt-edit-modal-title').textContent = title;
    this.editModal.querySelector('.prompt-edit-delete').style.display = 'block';

    // Fill form
    const form = this.editModal.querySelector('.prompt-edit-form');
    form.name.value = item.name;
    form.group.value = item.group;
    form.description.value = item.description || '';
    form.content.value = item.content;

    // Update save button color
    const saveBtn = this.editModal.querySelector('.prompt-edit-save');
    saveBtn.className = 'prompt-edit-save' + (type === 'skill' ? ' skill' : '');

    this.editModal.classList.add('open');
  }

  /**
   * Close the edit modal
   */
  closeEditModal() {
    this.editModal.classList.remove('open');
    this.editingItem = null;
    this.editingType = null;
  }

  /**
   * Save the item being edited/created
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

    const endpoint = this.editingType === 'prompt' ? '/api/prompts' : '/api/skills';
    const method = this.editingItem ? 'PATCH' : 'POST';
    const url = this.editingItem ? `${endpoint}/${this.editingItem.id}` : endpoint;

    try {
      const response = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });

      if (response.ok) {
        this.closeEditModal();
        // Reload data
        if (this.editingType === 'prompt') {
          await this.loadGroups();
          await this.loadPrompts();
        } else {
          await this.loadSkillGroups();
          await this.loadSkills();
        }
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
   * Delete the item being edited
   */
  async deleteEditingItem() {
    if (!this.editingItem) return;
    if (!confirm(`Delete this ${this.editingType}?`)) return;

    const endpoint = this.editingType === 'prompt' ? '/api/prompts' : '/api/skills';

    try {
      const response = await fetch(`${endpoint}/${this.editingItem.id}`, { method: 'DELETE' });
      if (response.ok) {
        this.closeEditModal();
        // Reload data
        if (this.editingType === 'prompt') {
          await this.loadGroups();
          await this.loadPrompts();
        } else {
          await this.loadSkillGroups();
          await this.loadSkills();
        }
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
   * Switch between prompts and skills tabs
   */
  switchTab(tabName) {
    this.activeTab = tabName;

    // Update tab button states
    this.panel.querySelectorAll('.prompt-panel-tab').forEach(tab => {
      tab.classList.toggle('active', tab.dataset.tab === tabName);
    });

    // Update search placeholder
    const searchInput = this.panel.querySelector('.prompt-search-input');
    searchInput.placeholder = tabName === 'prompts' ? 'Search prompts...' : 'Search skills...';
    searchInput.value = '';

    // Update add button style
    const addBtn = this.panel.querySelector('.prompt-panel-add');
    addBtn.className = 'prompt-panel-add' + (tabName === 'skills' ? ' skill' : '');

    // Load appropriate data
    this.loadCurrentTabGroups();
    this.loadCurrentTabData();
  }

  /**
   * Load groups for the current tab
   */
  async loadCurrentTabGroups() {
    if (this.activeTab === 'prompts') {
      await this.loadGroups();
    } else {
      await this.loadSkillGroups();
    }
  }

  /**
   * Load data for the current tab
   */
  async loadCurrentTabData() {
    if (this.activeTab === 'prompts') {
      await this.loadPrompts();
    } else {
      await this.loadSkills();
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

    await this.loadCurrentTabGroups();
    await this.loadCurrentTabData();

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
   * Load skill groups from API
   */
  async loadSkillGroups() {
    try {
      const response = await fetch('/api/skills/groups');
      const data = await response.json();
      this.skillGroups = data.groups;

      const select = this.panel.querySelector('.prompt-group-select');
      select.innerHTML = '<option value="">All Groups</option>';
      this.skillGroups.forEach(g => {
        const option = document.createElement('option');
        option.value = g;
        option.textContent = g;
        select.appendChild(option);
      });
    } catch (error) {
      console.error('Failed to load skill groups:', error);
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
   * Load skills from API
   */
  async loadSkills() {
    try {
      const search = this.panel.querySelector('.prompt-search-input').value;
      const group = this.panel.querySelector('.prompt-group-select').value;

      let url = '/api/skills';
      const params = new URLSearchParams();
      if (search) params.set('search', search);
      if (group) params.set('group', group);
      if (params.toString()) url += '?' + params.toString();

      const response = await fetch(url);
      this.skills = await response.json();
      this.renderSkills();
    } catch (error) {
      console.error('Failed to load skills:', error);
      this.panel.querySelector('.prompt-panel-list').innerHTML =
        '<div class="prompt-error">Failed to load skills</div>';
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
        if (prompt) this.showEditModal(prompt, 'prompt');
      });
    });
  }

  /**
   * Render skills in the panel
   */
  renderSkills() {
    const list = this.panel.querySelector('.prompt-panel-list');

    if (this.skills.length === 0) {
      list.innerHTML = `
        <div class="prompt-empty">
          <p>No skills found</p>
          <button class="prompt-create-btn skill" onclick="this.closest('.prompt-library-panel').querySelector('.prompt-panel-add').click()">
            <i class="fa-solid fa-plus"></i> Create your first skill
          </button>
        </div>
      `;
      return;
    }

    // Check which skills are selected
    const selectedIds = new Set(this.selectedSkills.map(s => s.id));

    list.innerHTML = this.skills.map(s => `
      <div class="skill-item ${selectedIds.has(s.id) ? 'selected' : ''}" data-id="${s.id}">
        <div class="skill-item-header">
          <span class="skill-item-checkbox">
            <i class="fa-${selectedIds.has(s.id) ? 'solid fa-check-square' : 'regular fa-square'}"></i>
          </span>
          <span class="skill-item-name">${this.escapeHtml(s.name)}</span>
          <button class="skill-item-edit" data-id="${s.id}" title="Edit">
            <i class="fa-solid fa-pen"></i>
          </button>
          <span class="skill-item-group">${this.escapeHtml(s.group)}</span>
        </div>
        ${s.description ? '<div class="skill-item-description">' + this.escapeHtml(s.description) + '</div>' : ''}
        <div class="skill-item-preview">${this.escapeHtml(s.content.substring(0, 100))}${s.content.length > 100 ? '...' : ''}</div>
      </div>
    `).join('');

    // Add click handlers for toggling
    list.querySelectorAll('.skill-item').forEach(item => {
      item.addEventListener('click', (e) => {
        // Don't toggle if clicking edit button
        if (e.target.closest('.skill-item-edit')) return;
        const id = parseInt(item.dataset.id);
        this.toggleSkill(id);
      });
    });

    // Add click handlers for edit buttons
    list.querySelectorAll('.skill-item-edit').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        const id = parseInt(btn.dataset.id);
        const skill = this.skills.find(s => s.id === id);
        if (skill) this.showEditModal(skill, 'skill');
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
   * Toggle a skill selection (multi-select)
   */
  async toggleSkill(id) {
    const skill = this.skills.find(s => s.id === id);
    if (!skill) return;

    const index = this.selectedSkills.findIndex(s => s.id === id);

    if (index >= 0) {
      // Deselect
      this.selectedSkills.splice(index, 1);
    } else {
      // Select
      this.selectedSkills.push(skill);

      // Track usage
      try {
        await fetch(`/api/skills/${id}/use`, { method: 'POST' });
      } catch (error) {
        console.error('Failed to track skill usage:', error);
      }
    }

    // Re-render the skills list to update checkboxes
    this.renderSkills();

    // Update the skills badge display
    this.showSelectedSkillsBadges();
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
   * Show badges for all selected skills
   */
  showSelectedSkillsBadges() {
    // Remove existing skills badge container
    if (this.skillsBadgeContainer) {
      this.skillsBadgeContainer.remove();
      this.skillsBadgeContainer = null;
    }

    if (this.selectedSkills.length === 0) {
      return;
    }

    // Create container for skill badges
    this.skillsBadgeContainer = document.createElement('div');
    this.skillsBadgeContainer.className = 'skills-selected-badges';

    this.selectedSkills.forEach(skill => {
      const badge = document.createElement('div');
      badge.className = 'skill-badge';
      badge.innerHTML = `
        <span class="skill-badge-icon"><i class="fa-solid fa-bolt"></i></span>
        <span class="skill-badge-text" title="${this.escapeHtml(skill.content)}">${this.escapeHtml(skill.name)}</span>
        <button class="skill-badge-close" data-id="${skill.id}" title="Remove skill">&times;</button>
      `;

      // Click on badge text to show expanded popup
      badge.querySelector('.skill-badge-text').addEventListener('click', (e) => {
        e.stopPropagation();
        this.showSkillExpandedPopup(skill, badge);
      });

      // Close button removes this skill
      badge.querySelector('.skill-badge-close').addEventListener('click', (e) => {
        e.stopPropagation();
        const id = parseInt(e.target.dataset.id);
        this.toggleSkill(id);
      });

      this.skillsBadgeContainer.appendChild(badge);
    });

    // Insert after prompt badge or before textarea
    if (this.messageInput && this.messageInput.parentElement) {
      const insertBefore = this.selectedBadge ?
        this.selectedBadge.nextSibling :
        this.messageInput;
      this.messageInput.parentElement.insertBefore(this.skillsBadgeContainer, insertBefore);
    }
  }

  /**
   * Show expanded popup for a skill
   */
  showSkillExpandedPopup(skill, badge) {
    // Remove any existing expanded popup
    const existingPopup = document.querySelector('.skill-expanded-popup');
    if (existingPopup) {
      existingPopup.remove();
    }

    const expandedPopup = document.createElement('div');
    expandedPopup.className = 'skill-expanded-popup open';
    expandedPopup.innerHTML = `
      <div class="skill-expanded-header">
        <span class="skill-expanded-title">${this.escapeHtml(skill.name)}</span>
        <button class="skill-expanded-close" title="Close">&times;</button>
      </div>
      <div class="skill-expanded-content">${this.escapeHtml(skill.content)}</div>
    `;

    badge.appendChild(expandedPopup);

    // Close handlers
    expandedPopup.querySelector('.skill-expanded-close').addEventListener('click', (e) => {
      e.stopPropagation();
      expandedPopup.remove();
    });

    // Close on outside click
    const closeHandler = (e) => {
      if (!expandedPopup.contains(e.target) && !badge.contains(e.target)) {
        expandedPopup.remove();
        document.removeEventListener('click', closeHandler);
      }
    };
    setTimeout(() => document.addEventListener('click', closeHandler), 0);
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
   * Clear all selected skills
   */
  clearSkillSelection() {
    this.selectedSkills = [];
    if (this.skillsBadgeContainer) {
      this.skillsBadgeContainer.remove();
      this.skillsBadgeContainer = null;
    }
    // Re-render if panel is open
    if (this.isOpen && this.activeTab === 'skills') {
      this.renderSkills();
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
   * Get selected skills content array
   */
  getSelectedSkillsContent() {
    return this.selectedSkills.map(s => s.content);
  }

  /**
   * Clear all selections after message is sent
   */
  clearSelection() {
    this.removeSelectedBadge();
    this.clearSkillSelection();
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
    this.clearSkillSelection();
    if (this.panel) {
      this.panel.remove();
    }
    if (this.editModal) {
      this.editModal.remove();
    }
  }
}
