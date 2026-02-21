/**
 * CheckpointManager - Manages git-based checkpoints for code rollback
 *
 * Provides UI for:
 * - Viewing checkpoint history for a thread
 * - Showing diffs for checkpoints
 * - Rolling back to previous checkpoints
 * - Accepting/rejecting AI changes
 */

import { DiffViewer } from './DiffViewer.js';

export class CheckpointManager {
  constructor(chatApp) {
    this.chatApp = chatApp;
    this.currentThreadId = null;
    this.checkpoints = [];
    this.currentHead = null;
    this.isVisible = false;
    this.diffViewer = null;

    this.initializeUI();
  }

  /**
   * Initialize the checkpoint panel UI
   */
  initializeUI() {
    // Get existing checkpoint button from header
    this.checkpointButton = document.querySelector('[data-llamabot="checkpoint-btn"]');
    if (this.checkpointButton) {
      this.checkpointButton.onclick = () => this.togglePanel();
    }

    // Create checkpoint panel (hidden by default)
    this.checkpointPanel = this.createCheckpointPanel();

    // Attach to DOM
    const chatSection = document.querySelector('.chat-section');
    if (chatSection) {
      chatSection.appendChild(this.checkpointPanel);
    }
  }

  /**
   * Create the checkpoint toggle button
   */
  createCheckpointButton() {
    const button = document.createElement('button');
    button.className = 'checkpoint-button';
    button.innerHTML = '🔖 Checkpoints';
    button.title = 'View and manage code checkpoints';
    button.onclick = () => this.togglePanel();
    return button;
  }

  /**
   * Create the checkpoint panel structure
   */
  createCheckpointPanel() {
    const panel = document.createElement('div');
    panel.className = 'checkpoint-panel hidden';
    panel.innerHTML = `
      <div class="checkpoint-panel-header">
        <h3><i class="fa-solid fa-clock-rotate-left"></i> History</h3>
        <button class="close-checkpoint-panel" title="Close">✕</button>
      </div>
      <div class="checkpoint-panel-content">
        <div class="checkpoint-list"></div>
        <div class="checkpoint-empty-state hidden">
          <p>No history yet</p>
          <small>History is saved automatically before AI edits</small>
        </div>
      </div>
    `;

    // Add close button handler
    const closeBtn = panel.querySelector('.close-checkpoint-panel');
    closeBtn.onclick = () => this.togglePanel();

    return panel;
  }

  /**
   * Toggle checkpoint panel visibility
   */
  togglePanel() {
    this.isVisible = !this.isVisible;

    if (this.isVisible) {
      this.checkpointPanel.classList.remove('hidden');
      this.fetchCheckpoints();
    } else {
      this.checkpointPanel.classList.add('hidden');
    }
  }

  /**
   * Fetch all checkpoints (not filtered by thread)
   */
  async fetchCheckpoints() {
    try {
      const response = await fetch('/api/checkpoints', {
        credentials: 'same-origin'  // Include cookies for session auth
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch checkpoints: ${response.statusText}`);
      }

      const data = await response.json();
      this.checkpoints = data.checkpoints || [];

      // Get current HEAD commit to mark which checkpoint we're on
      await this.fetchCurrentHead();

      this.renderCheckpointList();

    } catch (error) {
      console.error('Error fetching checkpoints:', error);
      this.showError('Failed to load checkpoints');
    }
  }

  /**
   * Fetch current git HEAD to identify which checkpoint is active
   */
  async fetchCurrentHead() {
    try {
      const response = await fetch('/api/checkpoints/current-head', {
        credentials: 'same-origin'
      });

      if (response.ok) {
        const data = await response.json();
        this.currentHead = data.head_sha;
      }
    } catch (error) {
      console.warn('Could not fetch current HEAD:', error);
      this.currentHead = null;
    }
  }

  /**
   * Render the checkpoint list
   */
  renderCheckpointList() {
    const listContainer = this.checkpointPanel.querySelector('.checkpoint-list');
    const emptyState = this.checkpointPanel.querySelector('.checkpoint-empty-state');

    if (this.checkpoints.length === 0) {
      listContainer.innerHTML = '';
      emptyState.classList.remove('hidden');
      return;
    }

    emptyState.classList.add('hidden');

    listContainer.innerHTML = this.checkpoints.map(checkpoint => {
      return this.renderCheckpointItem(checkpoint);
    }).join('');

    // Add event listeners to buttons
    this.attachCheckpointEventListeners();
  }

  /**
   * Render a single checkpoint item
   */
  renderCheckpointItem(checkpoint) {
    const timestamp = this.formatTimestamp(checkpoint.created_at);
    const fileCount = checkpoint.changed_files_count || 0;
    const isCurrent = this.currentHead && checkpoint.checkpoint_id.startsWith(this.currentHead);
    const currentIndicator = isCurrent ? '<i class="fa-solid fa-location-dot"></i> ' : '';

    return `
      <div class="checkpoint-item ${isCurrent ? 'current-checkpoint' : ''}" data-checkpoint-id="${checkpoint.checkpoint_id}">
        <div class="checkpoint-header">
          <span class="checkpoint-status">${currentIndicator}</span>
          <span class="checkpoint-description">${this.escapeHtml(checkpoint.description)}</span>
        </div>
        <div class="checkpoint-meta">
          <span class="checkpoint-time"><i class="fa-regular fa-clock"></i> ${timestamp}</span>
          <span class="checkpoint-files"><i class="fa-regular fa-file-code"></i> ${fileCount} file${fileCount !== 1 ? 's' : ''}</span>
        </div>
        <div class="checkpoint-actions">
          <button class="btn-rollback" data-checkpoint-id="${checkpoint.checkpoint_id}" ${isCurrent ? 'disabled' : ''}>
            <i class="fa-solid fa-check"></i> ${isCurrent ? 'Current' : 'Apply'}
          </button>
          <button class="btn-view-diff" data-checkpoint-id="${checkpoint.checkpoint_id}">
            <i class="fa-solid fa-code-compare"></i> View Changes
          </button>
        </div>
      </div>
    `;
  }

  /**
   * Attach event listeners to checkpoint action buttons
   */
  attachCheckpointEventListeners() {
    // View diff buttons
    this.checkpointPanel.querySelectorAll('.btn-view-diff').forEach(btn => {
      btn.onclick = () => {
        const checkpointId = btn.dataset.checkpointId;
        this.showDiff(checkpointId);
      };
    });

    // Rollback buttons
    this.checkpointPanel.querySelectorAll('.btn-rollback').forEach(btn => {
      btn.onclick = () => {
        const checkpointId = btn.dataset.checkpointId;
        this.confirmRollback(checkpointId);
      };
    });
  }

  /**
   * Show diff for a checkpoint
   */
  async showDiff(checkpointId) {
    try {
      const response = await fetch(`/api/checkpoints/${checkpointId}/diff`, {
        credentials: 'same-origin'  // Include cookies for session auth
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch diff: ${response.statusText}`);
      }

      const diffData = await response.json();

      // Create and show diff viewer modal
      if (!this.diffViewer) {
        this.diffViewer = new DiffViewer(this);
      }
      this.diffViewer.show(diffData);

    } catch (error) {
      console.error('Error fetching diff:', error);
      this.showError('Failed to load diff');
    }
  }

  /**
   * Confirm rollback with user
   */
  confirmRollback(checkpointId) {
    const checkpoint = this.checkpoints.find(cp => cp.checkpoint_id === checkpointId);
    const description = checkpoint ? checkpoint.description : 'this checkpoint';

    const confirmed = confirm(
      `Are you sure you want to rollback to "${description}"?\n\n` +
      `This will discard all changes made after this checkpoint.\n` +
      `This action cannot be undone.`
    );

    if (confirmed) {
      this.rollback(checkpointId);
    }
  }

  /**
   * Rollback to a checkpoint
   */
  async rollback(checkpointId) {
    try {
      const response = await fetch(`/api/checkpoints/${checkpointId}/rollback`, {
        method: 'POST',
        credentials: 'same-origin',  // Include cookies for session auth
        headers: {
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        throw new Error(`Rollback failed: ${response.statusText}`);
      }

      const data = await response.json();

      if (data.success) {
        // Automatically refresh Rails iframe to show rolled-back code
        if (this.chatApp.iframeManager) {
          this.chatApp.iframeManager.refreshRailsApp((callback) => this.chatApp.getRailsDebugInfo(callback));
        }

        this.showSuccess('Successfully rolled back changes');
        // Refresh checkpoint list
        this.fetchCheckpoints();
      } else {
        throw new Error('Rollback was not successful');
      }

    } catch (error) {
      console.error('Error during rollback:', error);
      this.showError('Failed to rollback: ' + error.message);
    }
  }

  /**
   * Accept a checkpoint (mark as accepted)
   */
  async acceptCheckpoint(checkpointId) {
    try {
      const response = await fetch(`/api/checkpoints/${checkpointId}/accept`, {
        method: 'POST',
        credentials: 'same-origin',  // Include cookies for session auth
        headers: {
          'Content-Type': 'application/json'
        }
      });

      if (!response.ok) {
        throw new Error(`Failed to accept checkpoint: ${response.statusText}`);
      }

      // Refresh checkpoint list to show updated status
      this.fetchCheckpoints();

    } catch (error) {
      console.error('Error accepting checkpoint:', error);
      this.showError('Failed to accept checkpoint');
    }
  }

  /**
   * Get status icon based on acceptance state
   */
  getStatusIcon(isAccepted) {
    if (isAccepted === true) return '✅';
    if (isAccepted === false) return '❌';
    return '⏳'; // Pending
  }

  /**
   * Format timestamp as relative time
   */
  formatTimestamp(isoString) {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins} min${diffMins > 1 ? 's' : ''} ago`;
    if (diffHours < 24) return `${diffHours} hour${diffHours > 1 ? 's' : ''} ago`;
    if (diffDays < 7) return `${diffDays} day${diffDays > 1 ? 's' : ''} ago`;

    return date.toLocaleDateString();
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
   * Show success message
   */
  showSuccess(message) {
    // Use ChatApp's notification system if available
    if (this.chatApp.showNotification) {
      this.chatApp.showNotification(message, 'success');
    } else {
      alert(message);
    }
  }

  /**
   * Show error message
   */
  showError(message) {
    // Use ChatApp's notification system if available
    if (this.chatApp.showNotification) {
      this.chatApp.showNotification(message, 'error');
    } else {
      alert('Error: ' + message);
    }
  }

  /**
   * Update when thread changes (no longer filters by thread)
   */
  onThreadChange(threadId) {
    this.currentThreadId = threadId;
    // Don't need to refresh - we show all checkpoints regardless of thread
  }

  /**
   * Refresh checkpoints (called after AI makes changes)
   */
  refresh() {
    this.fetchCheckpoints();
  }
}
