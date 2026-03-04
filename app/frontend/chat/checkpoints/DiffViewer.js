/**
 * DiffViewer - Modal for displaying git diffs
 *
 * Shows:
 * - File changes summary
 * - Full diff content with syntax highlighting
 * - Changed file list
 */

export class DiffViewer {
  constructor(checkpointManager) {
    this.checkpointManager = checkpointManager;
    this.modal = null;
    this.initializeModal();
  }

  /**
   * Initialize the diff viewer modal
   */
  initializeModal() {
    this.modal = document.createElement('div');
    this.modal.className = 'diff-viewer-modal hidden';
    this.modal.innerHTML = `
      <div class="diff-viewer-overlay"></div>
      <div class="diff-viewer-content">
        <div class="diff-viewer-header">
          <h3>📋 Code Changes</h3>
          <button class="close-diff-viewer" title="Close">✕</button>
        </div>
        <div class="diff-viewer-body">
          <div class="diff-summary"></div>
          <div class="diff-files-list"></div>
          <div class="diff-content"></div>
        </div>
        <div class="diff-viewer-footer">
          <button class="btn-close-diff">Close</button>
        </div>
      </div>
    `;

    // Add to DOM
    document.body.appendChild(this.modal);

    // Add event listeners
    this.modal.querySelector('.close-diff-viewer').onclick = () => this.hide();
    this.modal.querySelector('.btn-close-diff').onclick = () => this.hide();
    this.modal.querySelector('.diff-viewer-overlay').onclick = () => this.hide();

    // Prevent closing when clicking inside content
    this.modal.querySelector('.diff-viewer-content').onclick = (e) => e.stopPropagation();
  }

  /**
   * Show the diff viewer with diff data
   */
  show(diffData) {
    // Render diff content
    this.renderDiff(diffData);

    // Show modal
    this.modal.classList.remove('hidden');

    // Add escape key listener
    this.escapeKeyHandler = (e) => {
      if (e.key === 'Escape') {
        this.hide();
      }
    };
    document.addEventListener('keydown', this.escapeKeyHandler);
  }

  /**
   * Hide the diff viewer
   */
  hide() {
    this.modal.classList.add('hidden');

    // Remove escape key listener
    if (this.escapeKeyHandler) {
      document.removeEventListener('keydown', this.escapeKeyHandler);
      this.escapeKeyHandler = null;
    }
  }

  /**
   * Render the diff content
   */
  renderDiff(diffData) {
    // Render summary statistics
    this.renderSummary(diffData);

    // Render changed files list
    this.renderFilesList(diffData.changed_files || []);

    // Render full diff
    this.renderDiffContent(diffData.diff || '');
  }

  /**
   * Render summary statistics
   */
  renderSummary(diffData) {
    const summaryContainer = this.modal.querySelector('.diff-summary');
    const fileCount = (diffData.changed_files || []).length;

    summaryContainer.innerHTML = `
      <div class="diff-stats">
        <span class="stat-item">
          <strong>${fileCount}</strong> file${fileCount !== 1 ? 's' : ''} changed
        </span>
      </div>
    `;
  }

  /**
   * Render list of changed files
   */
  renderFilesList(changedFiles) {
    const filesContainer = this.modal.querySelector('.diff-files-list');

    if (changedFiles.length === 0) {
      filesContainer.innerHTML = '<p class="no-files">No files changed</p>';
      return;
    }

    const fileItems = changedFiles.map(file => {
      const icon = this.getFileIcon(file);
      return `
        <div class="diff-file-item">
          <span class="file-icon">${icon}</span>
          <span class="file-path">${this.escapeHtml(file)}</span>
        </div>
      `;
    }).join('');

    filesContainer.innerHTML = `
      <div class="diff-files-header">Changed Files:</div>
      <div class="diff-files">${fileItems}</div>
    `;
  }

  /**
   * Render the full diff content
   */
  renderDiffContent(diffText) {
    const diffContainer = this.modal.querySelector('.diff-content');

    if (!diffText || diffText.trim() === '') {
      diffContainer.innerHTML = '<p class="no-diff">No diff available</p>';
      return;
    }

    // Parse and syntax highlight the diff
    const highlightedDiff = this.highlightDiff(diffText);

    diffContainer.innerHTML = `
      <div class="diff-code-header">Diff:</div>
      <pre class="diff-code"><code>${highlightedDiff}</code></pre>
    `;
  }

  /**
   * Highlight diff content with basic syntax coloring
   */
  highlightDiff(diffText) {
    const lines = diffText.split('\n');

    return lines.map(line => {
      const escapedLine = this.escapeHtml(line);

      // Color lines based on diff syntax
      if (line.startsWith('+++') || line.startsWith('---')) {
        return `<span class="diff-file-marker">${escapedLine}</span>`;
      } else if (line.startsWith('+')) {
        return `<span class="diff-addition">${escapedLine}</span>`;
      } else if (line.startsWith('-')) {
        return `<span class="diff-deletion">${escapedLine}</span>`;
      } else if (line.startsWith('@@')) {
        return `<span class="diff-hunk-header">${escapedLine}</span>`;
      } else if (line.startsWith('diff --git')) {
        return `<span class="diff-header">${escapedLine}</span>`;
      } else if (line.startsWith('index ') || line.startsWith('new file') || line.startsWith('deleted file')) {
        return `<span class="diff-metadata">${escapedLine}</span>`;
      } else {
        return `<span class="diff-context">${escapedLine}</span>`;
      }
    }).join('\n');
  }

  /**
   * Get file icon based on file extension
   */
  getFileIcon(filePath) {
    const ext = filePath.split('.').pop().toLowerCase();

    const iconMap = {
      'js': '📜',
      'jsx': '⚛️',
      'ts': '📘',
      'tsx': '⚛️',
      'py': '🐍',
      'rb': '💎',
      'html': '🌐',
      'css': '🎨',
      'scss': '🎨',
      'json': '📋',
      'md': '📝',
      'yml': '⚙️',
      'yaml': '⚙️',
      'sh': '🔧',
      'sql': '🗄️',
      'env': '🔐',
    };

    return iconMap[ext] || '📄';
  }

  /**
   * Escape HTML to prevent XSS
   */
  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}
