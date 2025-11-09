/**
 * Tool message rendering with collapsible UI
 */

import { generateUniqueId } from '../utils/domHelpers.js';
import { PlanMessageRenderer } from './PlanMessageRenderer.js';
import { ToolIcons } from '../utils/icons.js';

export class ToolMessageRenderer {
  constructor(iframeManager = null, getRailsDebugInfoCallback = null) {
    this.iframeManager = iframeManager;
    this.getRailsDebugInfoCallback = getRailsDebugInfoCallback;
    this.planRenderer = new PlanMessageRenderer(iframeManager, getRailsDebugInfoCallback);
  }

  /**
   * Create collapsible tool message HTML
   */
  createCollapsibleToolMessage(toolName, firstArgument, toolArgs, toolResult) {
    const uniqueId = generateUniqueId('tool');

    // Special rendering for different tool types
    if (toolName === 'write_todos') {
      return this.renderTodoList(uniqueId, toolArgs);
    }

    if (toolName === 'edit_file') {
      return this.renderEditFile(uniqueId, firstArgument, toolArgs, toolResult);
    }

    // Default tool rendering
    return this.renderDefaultTool(uniqueId, toolName, firstArgument, toolArgs, toolResult);
  }

  /**
   * Render todo list tool using new plan-based renderer
   */
  renderTodoList(uniqueId, toolArgs) {
    const todos = JSON.parse(toolArgs)['todos'];

    // Use the new plan-based renderer for a sleeker UI
    // Note: uniqueId is generated in createCollapsibleToolMessage but not needed here
    // as PlanMessageRenderer generates its own unique IDs
    return this.planRenderer.createPlanMessage(
      'Task Plan',
      todos,
      { collapsible: true, showHeader: true }
    );
  }

  /**
   * Render edit file tool - minimal compact version
   */
  renderEditFile(uniqueId, firstArgument, toolArgs, toolResult) {
    const icon = ToolIcons.getIcon('edit_file');
    const filename = this._extractFilename(firstArgument);

    return `
      <div data-llamabot="tool-compact">
        ${icon}
        <span data-llamabot="tool-compact-name">Edit</span>
        <span data-llamabot="tool-compact-target">${this._escapeHtml(filename)}</span>
      </div>
    `;
  }

  /**
   * Render default tool - minimal compact version
   */
  renderDefaultTool(uniqueId, toolName, firstArgument, toolArgs, toolResult) {
    const icon = ToolIcons.getIcon(toolName);
    const displayName = this._formatToolName(toolName);
    const displayTarget = firstArgument ? this._extractFilename(firstArgument) : '';

    return `
      <div data-llamabot="tool-compact">
        ${icon}
        <span data-llamabot="tool-compact-name">${displayName}</span>
        ${displayTarget ? `<span data-llamabot="tool-compact-target">${this._escapeHtml(displayTarget)}</span>` : ''}
      </div>
    `;
  }

  /**
   * Format tool name for display (remove underscores, title case, remove "file" suffix)
   */
  _formatToolName(toolName) {
    // Special case for bash_command - just show "Bash"
    if (toolName === 'bash_command') {
      return 'Bash';
    }

    return toolName
      .replace(/_file$/, '')  // Remove "_file" suffix
      .split('_')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }

  /**
   * Extract filename from full path or clean up bash command
   */
  _extractFilename(path) {
    if (!path) return '';

    // For bash commands, strip out "bundle exec" prefix
    if (typeof path === 'string' && path.startsWith('bundle exec ')) {
      return path.replace(/^bundle exec /, '');
    }

    // Handle both forward and backward slashes
    const parts = path.replace(/\\/g, '/').split('/');
    return parts[parts.length - 1];
  }

  /**
   * Escape HTML to prevent XSS
   */
  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Update existing tool message with result (for compact format)
   */
  updateCollapsibleToolMessage(messageDiv, toolResult, baseMessage) {
    // Special handling for edit_file and write_file
    if (baseMessage.name === 'edit_file' || baseMessage.name === 'write_file') {
      const toolCompact = messageDiv.querySelector('[data-llamabot="tool-compact"]');
      if (toolCompact) {
        if (baseMessage.artifact?.status === 'success') {
          toolCompact.setAttribute('data-status', 'success');
          const icon = toolCompact.querySelector('[data-llamabot="tool-icon"]');
          if (icon) {
            icon.outerHTML = ToolIcons.successIcon();
          }

          // Refresh the main iframe when edit_file or write_file succeeds
          if (this.iframeManager && this.getRailsDebugInfoCallback) {
            this.iframeManager.refreshRailsApp(this.getRailsDebugInfoCallback);
          }
        } else if (baseMessage.artifact?.status === 'error') {
          toolCompact.setAttribute('data-status', 'error');
          const icon = toolCompact.querySelector('[data-llamabot="tool-icon"]');
          if (icon) {
            icon.outerHTML = ToolIcons.errorIcon();
          }
        }
      }
    }
  }
}

/**
 * Global functions for onclick handlers
 */
window.toggleToolCollapsible = function(contentId) {
  const collapsible = document.querySelector(`[onclick="toggleToolCollapsible('${contentId}')"]`);
  const content = document.getElementById(contentId);

  if (collapsible && content) {
    collapsible.classList.toggle('expanded');
    content.classList.toggle('expanded');
  }
};

window.toggleTodo = function(todoId) {
  event.stopPropagation();
  const todoText = document.getElementById(todoId);
  if (todoText) {
    todoText.classList.toggle('expanded');
  }
};
