/**
 * GitGraphRenderer - Renders a branch visualization rail for git commits
 *
 * Creates a visual representation of git branches similar to SourceTree/GitKraken
 * using pure CSS for the branch lines and commit nodes.
 */
export class GitGraphRenderer {
  constructor(checkpointManager) {
    this.checkpointManager = checkpointManager;
    this.commits = [];
    this.branches = [];
    this.maxBranchIndex = 0;
    this.branchColors = ['#8b5cf6', '#22c55e', '#eab308', '#3b82f6', '#ef4444', '#ec4899', '#14b8a6'];
    this.laneWidth = 16; // Width per lane in pixels
  }

  /**
   * Fetch git graph data from backend
   */
  async fetchGraphData() {
    try {
      const response = await fetch('/api/checkpoints/git-graph', {
        credentials: 'same-origin'
      });
      if (!response.ok) {
        // Try to extract error detail from response
        let errorDetail = `HTTP ${response.status}`;
        try {
          const errorData = await response.json();
          errorDetail = errorData.detail || errorDetail;
        } catch (e) {
          // Response wasn't JSON
        }
        const error = new Error(errorDetail);
        error.statusCode = response.status;
        throw error;
      }
      const data = await response.json();
      this.commits = data.commits || [];
      this.branches = data.branches || [];
      this.maxBranchIndex = data.max_branch_index || 0;
      return data;
    } catch (error) {
      console.error('Error fetching git graph:', error);
      // Return error info so caller can display it
      return { commits: [], branches: [], max_branch_index: 0, error: error.message };
    }
  }

  /**
   * Get the color for a branch lane
   */
  getBranchColor(branchIndex) {
    return this.branchColors[branchIndex % this.branchColors.length];
  }

  /**
   * Calculate the width needed for the graph rail
   */
  getGraphWidth() {
    return Math.max(32, (this.maxBranchIndex + 1) * this.laneWidth + 16);
  }

  /**
   * Render the graph rail for a single commit
   * @param {Object} commit - Commit data from API
   * @param {number} index - Index in the list (0 = most recent)
   * @param {number} totalCommits - Total number of commits
   * @param {Object} nextCommit - Next commit in list (older commit)
   * @param {Object} prevCommit - Previous commit in list (newer commit)
   * @returns {string} HTML for the graph rail
   */
  renderGraphRail(commit, index, totalCommits, nextCommit, prevCommit) {
    const isFirst = index === 0;
    const isLast = index === totalCommits - 1;
    const branchColor = this.getBranchColor(commit.branch_index);
    const laneX = commit.branch_index * this.laneWidth + 8;
    const graphWidth = this.getGraphWidth();

    let linesHtml = '';

    // Render vertical line for this commit's lane
    // Line goes from top to bottom of the row
    const topConnection = !isFirst;
    const bottomConnection = !isLast;

    if (topConnection || bottomConnection) {
      const topY = topConnection ? 0 : 50;
      const bottomY = bottomConnection ? 100 : 50;
      linesHtml += `
        <div class="git-graph-line" style="
          left: ${laneX}px;
          top: ${topY}%;
          bottom: ${100 - bottomY}%;
          background-color: ${branchColor};
        "></div>
      `;
    }

    // Render lines for other active lanes that pass through this row
    // We need to draw continuous lines for branches that aren't this commit
    for (let lane = 0; lane <= this.maxBranchIndex; lane++) {
      if (lane === commit.branch_index) continue;

      // Check if this lane has active commits above and below
      const hasCommitAbove = this.commits.slice(0, index).some(c => c.branch_index === lane);
      const hasCommitBelow = this.commits.slice(index + 1).some(c => c.branch_index === lane);

      if (hasCommitAbove && hasCommitBelow) {
        const otherLaneX = lane * this.laneWidth + 8;
        const otherColor = this.getBranchColor(lane);
        linesHtml += `
          <div class="git-graph-line" style="
            left: ${otherLaneX}px;
            top: 0;
            bottom: 0;
            background-color: ${otherColor};
          "></div>
        `;
      }
    }

    // Render merge lines (curved connectors from other lanes)
    if (commit.merge_lines && commit.merge_lines.length > 0) {
      for (const merge of commit.merge_lines) {
        const fromX = merge.from_lane * this.laneWidth + 8;
        const toX = merge.to_lane * this.laneWidth + 8;
        const mergeColor = this.getBranchColor(merge.to_lane);

        if (fromX !== toX) {
          // Diagonal merge line
          const minX = Math.min(fromX, toX);
          const maxX = Math.max(fromX, toX);
          const width = maxX - minX;
          const goingRight = toX > fromX;

          linesHtml += `
            <div class="git-graph-merge-line" style="
              left: ${minX}px;
              width: ${width}px;
              top: 50%;
              border-color: ${mergeColor};
              ${goingRight ? 'border-radius: 0 0 8px 0;' : 'border-radius: 0 0 0 8px;'}
            "></div>
          `;
        }
      }
    }

    // Render the commit node
    const nodeClass = commit.is_merge ? 'git-graph-node merge-node' : 'git-graph-node';
    const nodeHtml = `
      <div class="${nodeClass}"
           style="left: ${laneX}px; background-color: ${branchColor};"
           data-sha="${commit.sha}"
           data-short-sha="${commit.short_sha}"
           title="Click to copy: ${commit.short_sha}">
      </div>
    `;

    return `
      <div class="git-graph-rail" style="width: ${graphWidth}px;">
        ${linesHtml}
        ${nodeHtml}
      </div>
    `;
  }

  /**
   * Render commit info with truncated message
   */
  renderCommitInfo(commit) {
    const truncatedMessage = commit.subject.length > 40
      ? commit.subject.substring(0, 37) + '...'
      : commit.subject;

    const refsHtml = commit.refs && commit.refs.length > 0
      ? commit.refs.map(ref => `<span class="git-ref-badge">${this.escapeHtml(ref)}</span>`).join('')
      : '';

    return `
      <div class="git-commit-info">
        <div class="git-commit-refs">${refsHtml}</div>
        <span class="git-commit-message">${this.escapeHtml(truncatedMessage)}</span>
        <span class="git-commit-sha" data-sha="${commit.sha}" title="Click to copy">
          ${commit.short_sha}
        </span>
      </div>
    `;
  }

  /**
   * Attach click handlers for copying git hash
   */
  attachCopyHandlers(container) {
    container.querySelectorAll('.git-graph-node, .git-commit-sha').forEach(el => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        const sha = el.dataset.sha || el.dataset.shortSha;
        if (sha) {
          navigator.clipboard.writeText(sha).then(() => {
            this.showCopiedFeedback(el, sha.length > 10 ? 'Full SHA copied!' : 'Copied!');
          }).catch(err => {
            console.error('Failed to copy:', err);
          });
        }
      });
    });
  }

  /**
   * Show copied feedback tooltip
   */
  showCopiedFeedback(element, message = 'Copied!') {
    // Remove any existing feedback
    const existing = element.querySelector('.git-copied-feedback');
    if (existing) existing.remove();

    const feedback = document.createElement('div');
    feedback.className = 'git-copied-feedback';
    feedback.textContent = message;

    // Position relative to element
    element.style.position = 'relative';
    element.appendChild(feedback);

    setTimeout(() => feedback.remove(), 1500);
  }

  /**
   * Escape HTML to prevent XSS
   */
  escapeHtml(text) {
    if (!text) return '';
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
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
    if (diffMins < 60) return `${diffMins}m ago`;
    if (diffHours < 24) return `${diffHours}h ago`;
    if (diffDays < 7) return `${diffDays}d ago`;

    return date.toLocaleDateString();
  }
}
