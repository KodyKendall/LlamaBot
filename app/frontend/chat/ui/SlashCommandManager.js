/**
 * SlashCommandManager
 *
 * Manages the slash command dropdown in the chat input.
 * Shows available commands when user types "/" and executes them
 * with a confirmation dialog.
 */

export class SlashCommandManager {
  constructor(container = null) {
    this.container = container || document;
    this.messageInput = null;
    this.dropdown = null;
    this.commands = [];
    this.isOpen = false;
    this.selectedIndex = -1;
    this.confirmModal = null;
    this.chatApp = null; // Reference to ChatApp for adding messages
  }

  /**
   * Initialize the slash command manager
   * @param {HTMLElement} messageInput - The chat textarea
   * @param {Object} chatApp - Reference to the ChatApp instance (optional)
   */
  init(messageInput, chatApp = null) {
    this.messageInput = messageInput;
    this.chatApp = chatApp;

    if (!this.messageInput) {
      console.warn('Message input not found for SlashCommandManager');
      return;
    }

    this.createDropdown();
    this.createConfirmModal();
    this.attachEventListeners();
    this.fetchCommands();
  }

  /**
   * Create dropdown element
   */
  createDropdown() {
    this.dropdown = document.createElement('div');
    this.dropdown.className = 'slash-command-dropdown hidden';
    this.dropdown.setAttribute('data-llamabot', 'slash-command-dropdown');

    // Insert into the input area container
    const inputArea = this.messageInput.closest('.input-area') || this.messageInput.parentElement;
    inputArea.style.position = 'relative'; // Ensure positioning context
    inputArea.appendChild(this.dropdown);
  }

  /**
   * Create confirmation modal
   */
  createConfirmModal() {
    this.confirmModal = document.createElement('div');
    this.confirmModal.className = 'slash-command-modal hidden';
    this.confirmModal.innerHTML = `
      <div class="slash-command-modal-content">
        <div class="modal-header">
          <i class="fa-solid fa-terminal"></i>
          <span class="modal-title">Execute Command</span>
        </div>
        <div class="modal-body">
          <p class="modal-command"></p>
          <p class="modal-message"></p>
        </div>
        <div class="modal-actions">
          <button class="modal-cancel">Cancel</button>
          <button class="modal-confirm">Execute</button>
        </div>
      </div>
    `;

    document.body.appendChild(this.confirmModal);

    // Event listeners for modal
    this.confirmModal.querySelector('.modal-cancel').addEventListener('click', () => {
      this.hideConfirmModal();
    });

    this.confirmModal.addEventListener('click', (e) => {
      if (e.target === this.confirmModal) {
        this.hideConfirmModal();
      }
    });

    // Handle Escape key to close modal
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.confirmModal.classList.contains('hidden')) {
        this.hideConfirmModal();
      }
    });
  }

  /**
   * Attach input event listeners
   */
  attachEventListeners() {
    // Listen for input changes to detect "/"
    this.messageInput.addEventListener('input', () => {
      this.handleInput();
    });

    // Keyboard navigation
    this.messageInput.addEventListener('keydown', (e) => {
      if (!this.isOpen) return;

      switch (e.key) {
        case 'ArrowDown':
          e.preventDefault();
          this.selectNext();
          break;
        case 'ArrowUp':
          e.preventDefault();
          this.selectPrevious();
          break;
        case 'Enter':
          if (this.selectedIndex >= 0) {
            e.preventDefault();
            e.stopPropagation();
            this.executeSelected();
          }
          break;
        case 'Escape':
          e.preventDefault();
          this.hideDropdown();
          break;
        case 'Tab':
          if (this.selectedIndex >= 0) {
            e.preventDefault();
            this.autocomplete();
          }
          break;
      }
    });

    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
      if (!this.messageInput.contains(e.target) && !this.dropdown.contains(e.target)) {
        this.hideDropdown();
      }
    });
  }

  /**
   * Handle input changes
   */
  handleInput() {
    const value = this.messageInput.value;

    // Check if input starts with "/" and only contains command text (no spaces)
    if (value.startsWith('/') && !value.includes(' ')) {
      const query = value.slice(1).toLowerCase();
      this.showDropdown(query);
    } else {
      this.hideDropdown();
    }
  }

  /**
   * Fetch available commands from API
   */
  async fetchCommands() {
    try {
      const response = await fetch('/api/slash-commands');
      if (response.ok) {
        this.commands = await response.json();
      } else {
        console.error('Failed to fetch slash commands:', response.status);
        this.commands = [];
      }
    } catch (error) {
      console.error('Failed to fetch slash commands:', error);
      this.commands = [];
    }
  }

  /**
   * Show dropdown with filtered commands
   */
  showDropdown(query = '') {
    const filtered = this.commands.filter(cmd =>
      cmd.name.toLowerCase().includes(query)
    );

    if (filtered.length === 0) {
      this.hideDropdown();
      return;
    }

    this.dropdown.innerHTML = filtered.map((cmd, index) => `
      <div class="slash-command-item ${index === 0 ? 'selected' : ''}" data-command="${cmd.name}" data-index="${index}">
        <span class="command-name">/${cmd.name}</span>
        <span class="command-description">${cmd.description}</span>
        ${cmd.dangerous ? '<span class="command-warning"><i class="fa-solid fa-exclamation-triangle"></i></span>' : ''}
      </div>
    `).join('');

    // Add click handlers
    this.dropdown.querySelectorAll('.slash-command-item').forEach((item, index) => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        this.selectedIndex = index;
        this.executeSelected();
      });
      item.addEventListener('mouseenter', () => {
        this.setSelectedIndex(index);
      });
    });

    this.dropdown.classList.remove('hidden');
    this.isOpen = true;
    this.selectedIndex = 0;
    this.filteredCommands = filtered;
  }

  /**
   * Hide dropdown
   */
  hideDropdown() {
    this.dropdown.classList.add('hidden');
    this.isOpen = false;
    this.selectedIndex = -1;
    this.filteredCommands = [];
  }

  /**
   * Select next item in dropdown
   */
  selectNext() {
    const items = this.dropdown.querySelectorAll('.slash-command-item');
    if (items.length === 0) return;

    const newIndex = Math.min(this.selectedIndex + 1, items.length - 1);
    this.setSelectedIndex(newIndex);
  }

  /**
   * Select previous item in dropdown
   */
  selectPrevious() {
    const newIndex = Math.max(this.selectedIndex - 1, 0);
    this.setSelectedIndex(newIndex);
  }

  /**
   * Set selected index and update UI
   */
  setSelectedIndex(index) {
    const items = this.dropdown.querySelectorAll('.slash-command-item');
    items.forEach((item, i) => {
      item.classList.toggle('selected', i === index);
    });
    this.selectedIndex = index;

    // Scroll item into view if needed
    const selectedItem = items[index];
    if (selectedItem) {
      selectedItem.scrollIntoView({ block: 'nearest' });
    }
  }

  /**
   * Autocomplete the command in the input
   */
  autocomplete() {
    if (this.selectedIndex < 0 || !this.filteredCommands) return;

    const cmd = this.filteredCommands[this.selectedIndex];
    if (cmd) {
      this.messageInput.value = `/${cmd.name}`;
      this.hideDropdown();
    }
  }

  /**
   * Execute selected command (shows confirmation first)
   */
  executeSelected() {
    if (this.selectedIndex < 0 || !this.filteredCommands) return;

    const cmd = this.filteredCommands[this.selectedIndex];

    if (cmd) {
      this.showConfirmModal(cmd);
    }

    this.hideDropdown();
    this.messageInput.value = '';
  }

  /**
   * Show confirmation modal
   */
  showConfirmModal(command) {
    this.confirmModal.querySelector('.modal-command').textContent = `/${command.name}`;
    this.confirmModal.querySelector('.modal-message').textContent = command.confirm_message;

    const confirmBtn = this.confirmModal.querySelector('.modal-confirm');
    confirmBtn.className = `modal-confirm ${command.dangerous ? 'dangerous' : ''}`;

    // Remove old listener and add new one
    const newConfirmBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);

    newConfirmBtn.addEventListener('click', () => {
      this.executeCommand(command.name);
      this.hideConfirmModal();
    });

    this.confirmModal.classList.remove('hidden');
  }

  /**
   * Hide confirmation modal
   */
  hideConfirmModal() {
    this.confirmModal.classList.add('hidden');
  }

  /**
   * Execute command via API
   */
  async executeCommand(commandName) {
    try {
      // Show executing message in chat
      this.showSystemMessage(`Executing /${commandName}...`, 'info');

      const response = await fetch('/api/slash-commands/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: commandName })
      });

      const result = await response.json();

      if (result.success) {
        this.showSystemMessage(`/${commandName} completed successfully:\n\n${result.output}`, 'success');
      } else {
        this.showSystemMessage(`/${commandName} failed:\n\n${result.output}`, 'error');
      }

    } catch (error) {
      console.error('Failed to execute command:', error);
      this.showSystemMessage(`Error executing /${commandName}: ${error.message}`, 'error');
    }
  }

  /**
   * Show a system message in the chat
   */
  showSystemMessage(message, type = 'info') {
    // Create and dispatch a custom event that ChatApp can listen to
    const event = new CustomEvent('slashCommandMessage', {
      detail: { message, type }
    });
    window.dispatchEvent(event);

    // Also show as a notification toast
    this.showToast(message, type);
  }

  /**
   * Show a toast notification
   */
  showToast(message, type = 'info') {
    // Remove existing toasts
    const existingToasts = document.querySelectorAll('.slash-command-toast');
    existingToasts.forEach(t => t.remove());

    const toast = document.createElement('div');
    toast.className = `slash-command-toast ${type}`;

    // Truncate long messages for the toast
    const truncatedMessage = message.length > 200
      ? message.substring(0, 200) + '...'
      : message;

    toast.innerHTML = `
      <div class="toast-icon">
        ${type === 'success' ? '<i class="fa-solid fa-check-circle"></i>' : ''}
        ${type === 'error' ? '<i class="fa-solid fa-times-circle"></i>' : ''}
        ${type === 'info' ? '<i class="fa-solid fa-info-circle"></i>' : ''}
      </div>
      <div class="toast-message">${truncatedMessage.replace(/\n/g, '<br>')}</div>
      <button class="toast-close"><i class="fa-solid fa-times"></i></button>
    `;

    document.body.appendChild(toast);

    // Close button handler
    toast.querySelector('.toast-close').addEventListener('click', () => {
      toast.classList.add('hiding');
      setTimeout(() => toast.remove(), 300);
    });

    // Auto-remove after delay (longer for errors)
    const delay = type === 'error' ? 10000 : 5000;
    setTimeout(() => {
      if (toast.parentElement) {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 300);
      }
    }, delay);

    // Trigger animation
    requestAnimationFrame(() => {
      toast.classList.add('visible');
    });
  }
}
