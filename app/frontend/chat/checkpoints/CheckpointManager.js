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
import { GitGraphRenderer } from './GitGraphRenderer.js';

export class CheckpointManager {
  constructor(chatApp) {
    this.chatApp = chatApp;
    this.currentThreadId = null;
    this.checkpoints = [];
    this.currentHead = null;
    this.isVisible = false;
    this.diffViewer = null;
    this.badgeCheckInterval = null;
    this.gitGraphRenderer = new GitGraphRenderer(this);
    this.selectedCommit = null;
    this.graphData = null;

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

    // Get the unsaved badge element
    this.unsavedBadge = document.querySelector('[data-llamabot="unsaved-badge"]');

    // Create checkpoint panel (hidden by default)
    this.checkpointPanel = this.createCheckpointPanel();

    // Attach to DOM
    const chatSection = document.querySelector('.chat-section');
    if (chatSection) {
      chatSection.appendChild(this.checkpointPanel);
    }

    // Initial badge check and start periodic polling
    this.updateUnsavedBadge();
    this.startBadgePolling();
  }

  /**
   * Start polling for uncommitted changes (every 30 seconds)
   */
  startBadgePolling() {
    // Clear any existing interval
    if (this.badgeCheckInterval) {
      clearInterval(this.badgeCheckInterval);
    }

    // Poll every 30 seconds
    this.badgeCheckInterval = setInterval(() => {
      this.updateUnsavedBadge();
    }, 30000);
  }

  /**
   * Update the unsaved badge visibility based on uncommitted changes
   */
  async updateUnsavedBadge() {
    if (!this.unsavedBadge) return;

    try {
      const response = await fetch('/api/checkpoints/uncommitted', {
        credentials: 'same-origin'
      });

      if (response.ok) {
        const data = await response.json();
        if (data.has_changes) {
          this.unsavedBadge.classList.remove('hidden');
        } else {
          this.unsavedBadge.classList.add('hidden');
        }
      }
    } catch (error) {
      console.warn('Could not check for unsaved changes:', error);
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
        <button class="save-checkpoint-btn" title="Save checkpoint">
          <i class="fa-solid fa-plus"></i>
        </button>
        <button class="sync-github-btn" title="Push to remote">
          <i class="fa-solid fa-cloud-arrow-up"></i>
        </button>
        <button class="expand-history-btn" title="Open full git history">
          <i class="fa-solid fa-up-right-and-down-left-from-center"></i>
        </button>
        <button class="close-checkpoint-panel" title="Close">✕</button>
      </div>
      <div class="uncommitted-changes-banner hidden">
        <div class="uncommitted-changes-info" title="Click to see changed files">
          <i class="fa-solid fa-circle-exclamation"></i>
          <span class="uncommitted-changes-text">Unsaved changes</span>
          <i class="fa-solid fa-chevron-down uncommitted-expand-icon"></i>
        </div>
        <button class="btn-discard-changes" title="Discard all uncommitted changes">
          <i class="fa-solid fa-trash"></i> Discard
        </button>
      </div>
      <div class="uncommitted-file-list hidden"></div>
      <div class="save-checkpoint-form hidden">
        <input type="text" class="checkpoint-message-input" placeholder="Describe your changes...">
        <div class="save-checkpoint-actions">
          <button class="btn-save-checkpoint"><i class="fa-solid fa-check"></i> Save</button>
          <button class="btn-cancel-checkpoint">Cancel</button>
        </div>
      </div>
      <div class="checkpoint-panel-content">
        <div class="checkpoint-list"></div>
        <div class="checkpoint-empty-state hidden">
          <p>No history yet</p>
          <small>Click + to save a checkpoint</small>
        </div>
      </div>
      <div class="checkpoint-detail-panel hidden">
        <div class="checkpoint-detail-header">
          <span class="checkpoint-detail-sha" title="Click to copy"></span>
          <button class="checkpoint-detail-close" title="Close">
            <i class="fa-solid fa-xmark"></i>
          </button>
        </div>
        <div class="checkpoint-detail-body">
          <div class="checkpoint-detail-message"></div>
          <div class="checkpoint-detail-meta"></div>
          <div class="checkpoint-detail-files">
            <div class="checkpoint-detail-files-header">
              <i class="fa-regular fa-file-code"></i> Changed Files
            </div>
            <div class="checkpoint-detail-files-list"></div>
          </div>
        </div>
        <div class="checkpoint-detail-actions">
          <button class="btn-detail-rollback">
            <i class="fa-solid fa-rotate-left"></i> Rollback to this commit
          </button>
          <button class="btn-detail-diff">
            <i class="fa-solid fa-code-compare"></i> View Full Diff
          </button>
        </div>
      </div>
    `;

    // Add close button handler
    const closeBtn = panel.querySelector('.close-checkpoint-panel');
    closeBtn.onclick = () => this.togglePanel();

    // Add save checkpoint button handler
    const saveBtn = panel.querySelector('.save-checkpoint-btn');
    saveBtn.onclick = () => this.showSaveCheckpointForm();

    // Add sync to GitHub button handler
    const syncBtn = panel.querySelector('.sync-github-btn');
    syncBtn.onclick = () => this.syncToGitHub();

    // Add expand to full page button handler
    const expandBtn = panel.querySelector('.expand-history-btn');
    expandBtn.onclick = () => this.openFullHistoryPage();

    // Add discard changes button handler
    const discardBtn = panel.querySelector('.btn-discard-changes');
    discardBtn.onclick = () => this.discardUncommittedChanges();

    // Add uncommitted changes info click handler for expand/collapse
    const uncommittedInfo = panel.querySelector('.uncommitted-changes-info');
    uncommittedInfo.onclick = () => this.toggleUncommittedFileList();

    // Add form handlers
    const saveFormBtn = panel.querySelector('.btn-save-checkpoint');
    const cancelFormBtn = panel.querySelector('.btn-cancel-checkpoint');
    const messageInput = panel.querySelector('.checkpoint-message-input');

    saveFormBtn.onclick = () => this.saveCheckpoint();
    cancelFormBtn.onclick = () => this.hideSaveCheckpointForm();

    // Allow Enter key to submit
    messageInput.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        this.saveCheckpoint();
      } else if (e.key === 'Escape') {
        this.hideSaveCheckpointForm();
      }
    };

    // Add detail panel handlers
    const detailCloseBtn = panel.querySelector('.checkpoint-detail-close');
    detailCloseBtn.onclick = () => this.hideCommitDetail();

    const detailSha = panel.querySelector('.checkpoint-detail-sha');
    detailSha.onclick = () => {
      if (this.selectedCommit) {
        navigator.clipboard.writeText(this.selectedCommit.sha).then(() => {
          this.showSuccess('SHA copied to clipboard');
        });
      }
    };

    const detailRollbackBtn = panel.querySelector('.btn-detail-rollback');
    detailRollbackBtn.onclick = () => {
      if (this.selectedCommit) {
        this.confirmRollback(this.selectedCommit.sha);
      }
    };

    const detailDiffBtn = panel.querySelector('.btn-detail-diff');
    detailDiffBtn.onclick = () => {
      if (this.selectedCommit) {
        this.showDiff(this.selectedCommit.sha);
      }
    };

    return panel;
  }

  /**
   * Show the save checkpoint form
   */
  showSaveCheckpointForm() {
    const form = this.checkpointPanel.querySelector('.save-checkpoint-form');
    const input = this.checkpointPanel.querySelector('.checkpoint-message-input');
    form.classList.remove('hidden');
    input.value = '';
    input.focus();
  }

  /**
   * Hide the save checkpoint form
   */
  hideSaveCheckpointForm() {
    const form = this.checkpointPanel.querySelector('.save-checkpoint-form');
    form.classList.add('hidden');
  }

  /**
   * Save a new checkpoint with the user's message
   */
  async saveCheckpoint() {
    const input = this.checkpointPanel.querySelector('.checkpoint-message-input');
    const message = input.value.trim();

    if (!message) {
      this.showError('Please enter a description for your checkpoint');
      return;
    }

    try {
      const response = await fetch('/api/checkpoints', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          description: message
        })
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Failed to save checkpoint: ${response.statusText}`);
      }

      await response.json();

      // Hide form and refresh list
      this.hideSaveCheckpointForm();
      this.showSuccess(`Checkpoint saved: ${message}`);
      this.fetchCheckpoints();
      this.updateUnsavedBadge(); // Update badge after saving

    } catch (error) {
      console.error('Error saving checkpoint:', error);
      this.showError('Failed to save checkpoint: ' + error.message);
    }
  }

  /**
   * Toggle checkpoint panel visibility
   */
  togglePanel() {
    this.isVisible = !this.isVisible;

    if (this.isVisible) {
      this.checkpointPanel.classList.remove('hidden');
      this.fetchCheckpoints();
      this.checkUncommittedChanges();
    } else {
      this.checkpointPanel.classList.add('hidden');
    }
  }

  /**
   * Check for uncommitted changes and update the banner
   */
  async checkUncommittedChanges() {
    try {
      const response = await fetch('/api/checkpoints/uncommitted', {
        credentials: 'same-origin'
      });

      if (!response.ok) {
        throw new Error(`Failed to check uncommitted changes: ${response.statusText}`);
      }

      const data = await response.json();
      const banner = this.checkpointPanel.querySelector('.uncommitted-changes-banner');
      const textSpan = banner.querySelector('.uncommitted-changes-text');
      const fileList = this.checkpointPanel.querySelector('.uncommitted-file-list');

      if (data.has_changes) {
        const count = data.total_count;
        textSpan.textContent = `${count} unsaved change${count !== 1 ? 's' : ''}`;
        banner.classList.remove('hidden');

        // Render the file list
        this.renderUncommittedFileList(data.changed_files || [], data.untracked_files || []);
      } else {
        banner.classList.add('hidden');
        fileList.classList.add('hidden');
        fileList.innerHTML = '';
      }

    } catch (error) {
      console.warn('Could not check uncommitted changes:', error);
    }
  }

  /**
   * Render the list of uncommitted files
   */
  renderUncommittedFileList(changedFiles, untrackedFiles) {
    const fileList = this.checkpointPanel.querySelector('.uncommitted-file-list');

    const modifiedHtml = changedFiles.map(file => `
      <div class="uncommitted-file-item modified">
        <i class="fa-solid fa-pen"></i>
        <span class="uncommitted-file-name">${this.escapeHtml(file)}</span>
        <span class="uncommitted-file-type">modified</span>
      </div>
    `).join('');

    const newHtml = untrackedFiles.map(file => `
      <div class="uncommitted-file-item new">
        <i class="fa-solid fa-plus"></i>
        <span class="uncommitted-file-name">${this.escapeHtml(file)}</span>
        <span class="uncommitted-file-type">new file</span>
      </div>
    `).join('');

    fileList.innerHTML = modifiedHtml + newHtml;
  }

  /**
   * Toggle the uncommitted file list visibility
   */
  toggleUncommittedFileList() {
    const fileList = this.checkpointPanel.querySelector('.uncommitted-file-list');
    const expandIcon = this.checkpointPanel.querySelector('.uncommitted-expand-icon');

    if (fileList.classList.contains('hidden')) {
      fileList.classList.remove('hidden');
      expandIcon.classList.remove('fa-chevron-down');
      expandIcon.classList.add('fa-chevron-up');
    } else {
      fileList.classList.add('hidden');
      expandIcon.classList.remove('fa-chevron-up');
      expandIcon.classList.add('fa-chevron-down');
    }
  }

  /**
   * Discard all uncommitted changes
   */
  async discardUncommittedChanges() {
    // Confirm with user
    if (!confirm('Are you sure you want to discard all uncommitted changes? This cannot be undone.')) {
      return;
    }

    try {
      const response = await fetch('/api/checkpoints/discard', {
        method: 'POST',
        credentials: 'same-origin'
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Failed to discard changes: ${response.statusText}`);
      }

      const data = await response.json();

      // Refresh the Rails iframe to show reverted state
      if (this.chatApp.iframeManager) {
        this.chatApp.iframeManager.refreshRailsApp((callback) => this.chatApp.getRailsDebugInfo(callback));
      }

      this.showSuccess(data.message);
      this.checkUncommittedChanges(); // Update banner
      this.updateUnsavedBadge(); // Update badge after discarding

    } catch (error) {
      console.error('Error discarding changes:', error);
      this.showError('Failed to discard changes: ' + error.message);
    }
  }

  /**
   * Sync commits to GitHub (git push)
   */
  async syncToGitHub() {
    // Confirm with user
    if (!confirm('Push all commits to GitHub? This will sync your local changes with the remote repository.')) {
      return;
    }

    try {
      const syncBtn = this.checkpointPanel.querySelector('.sync-github-btn');
      syncBtn.disabled = true;
      syncBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

      const response = await fetch('/api/git/push', {
        method: 'POST',
        credentials: 'same-origin'
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || `Failed to push: ${response.statusText}`);
      }

      const data = await response.json();

      if (data.success) {
        this.showSuccess(data.message);
        // Refresh the uncommitted changes banner to reflect current state
        await this.checkUncommittedChanges();
      } else {
        this.showError(data.message || 'Failed to push to GitHub');
      }

    } catch (error) {
      console.error('Error pushing to GitHub:', error);
      this.showError('Failed to push to GitHub: ' + error.message);
    } finally {
      const syncBtn = this.checkpointPanel.querySelector('.sync-github-btn');
      syncBtn.disabled = false;
      syncBtn.innerHTML = '<i class="fa-solid fa-cloud-arrow-up"></i>';
    }
  }

  /**
   * Fetch all checkpoints (not filtered by thread)
   */
  async fetchCheckpoints() {
    try {
      // Fetch both checkpoints and git graph data in parallel
      const [checkpointsResponse, graphData] = await Promise.all([
        fetch('/api/checkpoints', { credentials: 'same-origin' }),
        this.gitGraphRenderer.fetchGraphData()
      ]);

      if (!checkpointsResponse.ok) {
        throw new Error(`Failed to fetch checkpoints: ${checkpointsResponse.statusText}`);
      }

      const data = await checkpointsResponse.json();
      this.checkpoints = data.checkpoints || [];
      this.graphData = graphData;

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

    // Check for errors in graph data
    if (this.graphData?.error) {
      listContainer.innerHTML = `
        <div class="checkpoint-error-state">
          <i class="fa-solid fa-circle-exclamation"></i>
          <p>Failed to load git history</p>
          <pre class="error-detail">${this.escapeHtml(this.graphData.error)}</pre>
          <button class="btn-retry-fetch" onclick="window.checkpointManager?.fetchCheckpoints()">
            <i class="fa-solid fa-rotate-right"></i> Retry
          </button>
        </div>
      `;
      emptyState.classList.add('hidden');
      return;
    }

    // Use graph data commits if available, otherwise fall back to checkpoints
    const commits = this.graphData?.commits || [];
    const totalCommits = commits.length;

    if (totalCommits === 0 && this.checkpoints.length === 0) {
      listContainer.innerHTML = '';
      emptyState.classList.remove('hidden');
      return;
    }

    emptyState.classList.add('hidden');

    // Render using graph data for visualization
    if (totalCommits > 0) {
      listContainer.innerHTML = commits.map((commit, index) => {
        const nextCommit = index < totalCommits - 1 ? commits[index + 1] : null;
        const prevCommit = index > 0 ? commits[index - 1] : null;
        return this.renderGraphCommitItem(commit, index, totalCommits, nextCommit, prevCommit);
      }).join('');
    } else {
      // Fallback to old checkpoint rendering
      listContainer.innerHTML = this.checkpoints.map(checkpoint => {
        return this.renderCheckpointItem(checkpoint);
      }).join('');
    }

    // Add event listeners to buttons
    this.attachCheckpointEventListeners();

    // Attach git hash copy handlers
    this.gitGraphRenderer.attachCopyHandlers(listContainer);
  }

  /**
   * Render a single checkpoint item (legacy fallback)
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
          <span class="checkpoint-files-toggle" data-checkpoint-id="${checkpoint.checkpoint_id}">
            <i class="fa-regular fa-file-code"></i> ${fileCount} file${fileCount !== 1 ? 's' : ''} <i class="fa-solid fa-chevron-down expand-icon"></i>
          </span>
        </div>
        <div class="checkpoint-file-list hidden" data-checkpoint-id="${checkpoint.checkpoint_id}">
          <div class="checkpoint-file-list-loading">Loading...</div>
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
   * Render a commit item as a simple linear list (no branch visualization)
   */
  renderGraphCommitItem(commit, index, totalCommits, nextCommit, prevCommit) {
    const timestamp = this.gitGraphRenderer.formatTimestamp(commit.timestamp);
    const fileCount = commit.changed_files_count || 0;
    const isCurrent = this.currentHead && commit.sha.startsWith(this.currentHead);

    // Truncate commit message
    const truncatedMessage = commit.subject.length > 50
      ? commit.subject.substring(0, 47) + '...'
      : commit.subject;

    // Render refs (branch/tag badges)
    const refsHtml = commit.refs && commit.refs.length > 0
      ? commit.refs.slice(0, 3).map(ref => `<span class="git-ref-badge">${this.escapeHtml(ref)}</span>`).join('')
      : '';

    return `
      <div class="checkpoint-item git-commit-item ${isCurrent ? 'current-checkpoint' : ''}"
           data-checkpoint-id="${commit.sha}"
           data-commit-index="${index}">
        <div class="checkpoint-content">
          <div class="checkpoint-header">
            ${isCurrent ? '<i class="fa-solid fa-location-dot current-indicator"></i>' : ''}
            <span class="checkpoint-description" title="${this.escapeHtml(commit.subject)}">
              ${this.escapeHtml(truncatedMessage)}
            </span>
          </div>
          <div class="git-commit-refs-row">${refsHtml}</div>
          <div class="checkpoint-meta">
            <span class="checkpoint-time"><i class="fa-regular fa-clock"></i> ${timestamp}</span>
            <span class="git-commit-sha-badge" data-sha="${commit.sha}" title="Click to copy">
              ${commit.short_sha}
            </span>
            <span class="checkpoint-files-count">
              <i class="fa-regular fa-file-code"></i> ${fileCount}
            </span>
          </div>
        </div>
      </div>
    `;
  }

  /**
   * Attach event listeners to checkpoint action buttons
   */
  attachCheckpointEventListeners() {
    // File list toggle (for legacy items)
    this.checkpointPanel.querySelectorAll('.checkpoint-files-toggle').forEach(toggle => {
      toggle.onclick = () => {
        const checkpointId = toggle.dataset.checkpointId;
        this.toggleFileList(checkpointId, toggle);
      };
    });

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

    // Git commit items - click to show detail panel
    this.checkpointPanel.querySelectorAll('.git-commit-item').forEach(item => {
      item.onclick = (e) => {
        // Don't trigger if clicking on the graph node (which copies SHA)
        if (e.target.closest('.git-graph-node') || e.target.closest('.git-commit-sha-badge')) {
          return;
        }
        const commitIndex = parseInt(item.dataset.commitIndex, 10);
        if (this.graphData?.commits && this.graphData.commits[commitIndex]) {
          this.showCommitDetail(this.graphData.commits[commitIndex]);
        }
      };
    });

    // SHA badge click to copy
    this.checkpointPanel.querySelectorAll('.git-commit-sha-badge').forEach(badge => {
      badge.onclick = (e) => {
        e.stopPropagation();
        const sha = badge.dataset.sha;
        if (sha) {
          navigator.clipboard.writeText(sha).then(() => {
            this.showSuccess('SHA copied to clipboard');
          });
        }
      };
    });
  }

  /**
   * Show commit detail panel with full message and file list
   */
  async showCommitDetail(commit) {
    this.selectedCommit = commit;

    const detailPanel = this.checkpointPanel.querySelector('.checkpoint-detail-panel');
    const shaEl = detailPanel.querySelector('.checkpoint-detail-sha');
    const messageEl = detailPanel.querySelector('.checkpoint-detail-message');
    const metaEl = detailPanel.querySelector('.checkpoint-detail-meta');
    const filesListEl = detailPanel.querySelector('.checkpoint-detail-files-list');

    // Populate SHA
    shaEl.textContent = commit.short_sha;
    shaEl.dataset.sha = commit.sha;

    // Populate message
    messageEl.textContent = commit.subject;

    // Populate meta
    const timestamp = this.gitGraphRenderer.formatTimestamp(commit.timestamp);
    metaEl.innerHTML = `
      <div class="detail-meta-row">
        <i class="fa-regular fa-user"></i> ${this.escapeHtml(commit.author)}
      </div>
      <div class="detail-meta-row">
        <i class="fa-regular fa-clock"></i> ${timestamp}
      </div>
    `;

    // Show loading state for files
    filesListEl.innerHTML = '<div class="loading-files">Loading files...</div>';

    // Show the panel
    detailPanel.classList.remove('hidden');
    detailPanel.classList.add('visible');

    // Highlight selected item
    this.checkpointPanel.querySelectorAll('.checkpoint-item').forEach(item => {
      item.classList.remove('selected');
    });
    const selectedItem = this.checkpointPanel.querySelector(`[data-checkpoint-id="${commit.sha}"]`);
    if (selectedItem) {
      selectedItem.classList.add('selected');
    }

    // Fetch and display changed files
    try {
      const response = await fetch(`/api/checkpoints/${commit.sha}/diff`, {
        credentials: 'same-origin'
      });

      if (response.ok) {
        const diffData = await response.json();
        const files = diffData.changed_files || [];

        if (files.length === 0) {
          filesListEl.innerHTML = '<div class="no-files">No files changed</div>';
        } else {
          filesListEl.innerHTML = files.map(file => `
            <div class="detail-file-item">
              <i class="fa-regular fa-file-code"></i>
              <span class="detail-file-name">${this.escapeHtml(file)}</span>
            </div>
          `).join('');
        }
      } else {
        filesListEl.innerHTML = '<div class="error-files">Failed to load files</div>';
      }
    } catch (error) {
      console.error('Error loading commit files:', error);
      filesListEl.innerHTML = '<div class="error-files">Failed to load files</div>';
    }
  }

  /**
   * Hide commit detail panel
   */
  hideCommitDetail() {
    this.selectedCommit = null;

    const detailPanel = this.checkpointPanel.querySelector('.checkpoint-detail-panel');
    detailPanel.classList.remove('visible');
    detailPanel.classList.add('hidden');

    // Remove selection highlight
    this.checkpointPanel.querySelectorAll('.checkpoint-item').forEach(item => {
      item.classList.remove('selected');
    });
  }

  /**
   * Toggle the file list for a checkpoint
   */
  async toggleFileList(checkpointId, toggleElement) {
    const fileList = this.checkpointPanel.querySelector(`.checkpoint-file-list[data-checkpoint-id="${checkpointId}"]`);
    const expandIcon = toggleElement.querySelector('.expand-icon');

    if (fileList.classList.contains('hidden')) {
      // Expand - show file list
      fileList.classList.remove('hidden');
      expandIcon.classList.remove('fa-chevron-down');
      expandIcon.classList.add('fa-chevron-up');

      // Fetch files if not already loaded
      if (fileList.querySelector('.checkpoint-file-list-loading')) {
        await this.loadFileList(checkpointId, fileList);
      }
    } else {
      // Collapse
      fileList.classList.add('hidden');
      expandIcon.classList.remove('fa-chevron-up');
      expandIcon.classList.add('fa-chevron-down');
    }
  }

  /**
   * Load the list of changed files for a checkpoint
   */
  async loadFileList(checkpointId, fileListElement) {
    try {
      const response = await fetch(`/api/checkpoints/${checkpointId}/diff`, {
        credentials: 'same-origin'
      });

      if (!response.ok) {
        throw new Error(`Failed to fetch files: ${response.statusText}`);
      }

      const data = await response.json();
      const files = data.changed_files || [];

      if (files.length === 0) {
        fileListElement.innerHTML = '<div class="checkpoint-file-item no-files">No files changed</div>';
      } else {
        fileListElement.innerHTML = files.map(file => `
          <div class="checkpoint-file-item">
            <i class="fa-regular fa-file-code"></i>
            <span class="checkpoint-file-name">${this.escapeHtml(file)}</span>
          </div>
        `).join('');
      }

    } catch (error) {
      console.error('Error loading file list:', error);
      fileListElement.innerHTML = '<div class="checkpoint-file-item error">Failed to load files</div>';
    }
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

  /**
   * Open full-page git history view in a new tab
   */
  openFullHistoryPage() {
    window.open('/git-history', '_blank');
  }
}
