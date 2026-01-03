/**
 * Tool message rendering with collapsible UI
 */

import { generateUniqueId } from '../utils/domHelpers.js';
import { PlanMessageRenderer } from './PlanMessageRenderer.js';
import { ToolIcons } from '../utils/icons.js';

// Tools that should be expandable to show args and output
const EXPANDABLE_TOOLS = ['grep_files', 'glob_files', 'bash_command', 'delegate_task'];

// Tools that should be expandable but only show input args (no output)
const INPUT_ONLY_EXPANDABLE_TOOLS = ['read_file', 'edit_file'];

export class ToolMessageRenderer {
  constructor(iframeManager = null, getRailsDebugInfoCallback = null) {
    this.iframeManager = iframeManager;
    this.getRailsDebugInfoCallback = getRailsDebugInfoCallback;
    this.planRenderer = new PlanMessageRenderer(iframeManager, getRailsDebugInfoCallback);
    // Store tool data for expandable tools
    this.toolDataStore = new Map();
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

    if (INPUT_ONLY_EXPANDABLE_TOOLS.includes(toolName)) {
      return this.renderInputOnlyExpandable(uniqueId, toolName, firstArgument, toolArgs);
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
   * Render tools that only show file path when expanded (no output)
   * Used for read_file, edit_file, etc.
   */
  renderInputOnlyExpandable(uniqueId, toolName, _firstArgument, toolArgs) {
    const icon = ToolIcons.getIcon(toolName);
    const displayName = this._formatToolName(toolName);

    // Extract the file_path from args (more reliable than firstArgument for edit_file)
    const filePath = this._extractFilePath(toolArgs);
    const displayTarget = filePath ? this._extractFilename(filePath) : '';

    return `
      <div data-llamabot="tool-expandable" data-tool-id="${uniqueId}" data-input-only="true" onclick="toggleToolExpand('${uniqueId}')">
        <div data-llamabot="tool-compact" data-expandable="true">
          ${icon}
          <span data-llamabot="tool-compact-name">${displayName}</span>
          ${displayTarget ? `<span data-llamabot="tool-compact-target">${this._escapeHtml(displayTarget)}</span>` : ''}
          <span data-llamabot="tool-expand-arrow">▶</span>
        </div>
        <div data-llamabot="tool-expand-content" id="tool-content-${uniqueId}">
          <div data-llamabot="tool-expand-section">
            <div data-llamabot="tool-expand-label">Path:</div>
            <pre data-llamabot="tool-expand-code">${this._escapeHtml(filePath)}</pre>
          </div>
        </div>
      </div>
    `;
  }

  /**
   * Extract file_path from tool args
   */
  _extractFilePath(toolArgs) {
    try {
      const args = typeof toolArgs === 'string' ? JSON.parse(toolArgs) : toolArgs;
      return args.file_path || args.path || '';
    } catch (e) {
      return '';
    }
  }

  /**
   * Render default tool - minimal compact version
   * For expandable tools (Grep, Glob, Bash), make them clickable to show args/output
   */
  renderDefaultTool(uniqueId, toolName, firstArgument, toolArgs, toolResult) {
    const icon = ToolIcons.getIcon(toolName);
    const displayName = this._formatToolName(toolName);
    const displayTarget = firstArgument ? this._extractFilename(firstArgument) : '';
    const isExpandable = EXPANDABLE_TOOLS.includes(toolName);

    if (isExpandable) {
      // Store the tool data for later retrieval
      this.toolDataStore.set(uniqueId, {
        toolName,
        toolArgs,
        toolResult: toolResult || ''
      });

      return `
        <div data-llamabot="tool-expandable" data-tool-id="${uniqueId}" onclick="toggleToolExpand('${uniqueId}')">
          <div data-llamabot="tool-compact" data-expandable="true">
            ${icon}
            <span data-llamabot="tool-compact-name">${displayName}</span>
            ${displayTarget ? `<span data-llamabot="tool-compact-target">${this._escapeHtml(displayTarget)}</span>` : ''}
            <span data-llamabot="tool-expand-arrow">▶</span>
          </div>
          <div data-llamabot="tool-expand-content" id="tool-content-${uniqueId}">
            <div data-llamabot="tool-expand-section">
              <div data-llamabot="tool-expand-label">Input:</div>
              <pre data-llamabot="tool-expand-code">${this._escapeHtml(this._formatToolArgs(toolArgs))}</pre>
            </div>
            <div data-llamabot="tool-expand-section" data-llamabot-output="true">
              <div data-llamabot="tool-expand-label">Output:</div>
              <pre data-llamabot="tool-expand-code" data-llamabot="tool-output-content">Loading...</pre>
            </div>
          </div>
        </div>
      `;
    }

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
   * Format tool args for display
   */
  _formatToolArgs(toolArgs) {
    try {
      const args = typeof toolArgs === 'string' ? JSON.parse(toolArgs) : toolArgs;
      // Format nicely with indentation
      return JSON.stringify(args, null, 2);
    } catch (e) {
      return String(toolArgs);
    }
  }

  /**
   * Truncate output if too long
   */
  _truncateOutput(output, maxLength = 2000) {
    if (!output) return '(no output)';
    const str = String(output);
    if (str.length > maxLength) {
      return str.substring(0, maxLength) + '\n\n... (truncated)';
    }
    return str;
  }

  /**
   * Update existing tool message with result (for compact format)
   */
  updateCollapsibleToolMessage(messageDiv, toolResult, baseMessage) {
    // Handle expandable tools (Grep, Glob, Bash) - update their output
    if (EXPANDABLE_TOOLS.includes(baseMessage.name)) {
      const expandableDiv = messageDiv.querySelector('[data-llamabot="tool-expandable"]');
      if (expandableDiv) {
        const toolId = expandableDiv.getAttribute('data-tool-id');
        const outputSection = expandableDiv.querySelector('[data-llamabot-output="true"]');

        if (outputSection) {
          const outputContent = outputSection.querySelector('pre');
          if (outputContent) {
            outputContent.textContent = this._truncateOutput(toolResult);
          }
        }

        // Update stored data
        if (toolId && this.toolDataStore.has(toolId)) {
          const data = this.toolDataStore.get(toolId);
          data.toolResult = toolResult;
        }

        // Update status styling
        const toolCompact = expandableDiv.querySelector('[data-llamabot="tool-compact"]');
        if (toolCompact) {
          if (baseMessage.artifact?.status === 'success') {
            toolCompact.setAttribute('data-status', 'success');
          } else if (baseMessage.artifact?.status === 'error') {
            toolCompact.setAttribute('data-status', 'error');
          }
        }
      }
      return;
    }

    // Handle input-only expandable tools (read_file, edit_file) - just update status
    if (INPUT_ONLY_EXPANDABLE_TOOLS.includes(baseMessage.name)) {
      const expandableDiv = messageDiv.querySelector('[data-llamabot="tool-expandable"]');
      if (expandableDiv) {
        const toolCompact = expandableDiv.querySelector('[data-llamabot="tool-compact"]');
        if (toolCompact) {
          if (baseMessage.artifact?.status === 'success') {
            toolCompact.setAttribute('data-status', 'success');
            const icon = toolCompact.querySelector('[data-llamabot="tool-icon"]');
            if (icon) {
              icon.outerHTML = ToolIcons.successIcon();
            }

            // Refresh the main iframe when edit_file or write_file succeeds
            if ((baseMessage.name === 'edit_file' || baseMessage.name === 'write_file') &&
                this.iframeManager && this.getRailsDebugInfoCallback) {
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
      return;
    }

    // Special handling for write_file (not in INPUT_ONLY_EXPANDABLE_TOOLS)
    if (baseMessage.name === 'write_file') {
      const toolCompact = messageDiv.querySelector('[data-llamabot="tool-compact"]');
      if (toolCompact) {
        if (baseMessage.artifact?.status === 'success') {
          toolCompact.setAttribute('data-status', 'success');
          const icon = toolCompact.querySelector('[data-llamabot="tool-icon"]');
          if (icon) {
            icon.outerHTML = ToolIcons.successIcon();
          }

          // Refresh the main iframe when write_file succeeds
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

/**
 * Toggle expandable tool (Grep, Glob, Bash) to show/hide args and output
 */
window.toggleToolExpand = function(toolId) {
  const expandableDiv = document.querySelector(`[data-tool-id="${toolId}"]`);
  if (expandableDiv) {
    expandableDiv.classList.toggle('expanded');
  }
};
