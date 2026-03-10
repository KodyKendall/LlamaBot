/**
 * Tool message rendering with collapsible UI
 */

import { generateUniqueId } from '../utils/domHelpers.js';
import { PlanMessageRenderer } from './PlanMessageRenderer.js';
import { ToolIcons } from '../utils/icons.js';

// Tools that should be expandable to show args and output
const EXPANDABLE_TOOLS = ['grep_files', 'glob_files', 'bash_command', 'delegate_task', 'delegate_research'];

// Tools that should be expandable but only show input args (no output)
const INPUT_ONLY_EXPANDABLE_TOOLS = ['read_file', 'edit_file', 'write_file'];

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
   * @param {string} toolName - Name of the tool
   * @param {string} firstArgument - First argument for display
   * @param {string} toolArgs - Tool arguments as JSON string
   * @param {string} toolResult - Tool result (optional)
   * @param {number} agentDepth - Depth of the agent (0 = main, 1+ = sub-agent)
   */
  createCollapsibleToolMessage(toolName, firstArgument, toolArgs, toolResult, agentDepth = 0) {
    const uniqueId = generateUniqueId('tool');

    // Special rendering for different tool types
    if (toolName === 'write_todos') {
      return this.renderTodoList(uniqueId, toolArgs, agentDepth);
    }

    if (INPUT_ONLY_EXPANDABLE_TOOLS.includes(toolName)) {
      return this.renderInputOnlyExpandable(uniqueId, toolName, firstArgument, toolArgs, agentDepth);
    }

    // Default tool rendering
    return this.renderDefaultTool(uniqueId, toolName, firstArgument, toolArgs, toolResult, agentDepth);
  }

  /**
   * Render sub-agent badge HTML
   * @param {number} depth - Agent depth (0 = main agent, 1+ = sub-agent)
   * @returns {string} HTML for the badge, or empty string if main agent
   */
  _renderSubagentBadge(depth) {
    if (depth === 0) return '';

    // Show depth number only for depth > 1 (nested sub-agents)
    const depthIndicator = depth > 1 ? `<span class="depth-num">${depth}</span>` : '';

    return `
      <span class="subagent-badge" data-depth="${depth}" title="Sub-agent depth ${depth}">
        <svg width="8" height="8" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" fill="currentColor"/></svg>
        ${depthIndicator}
      </span>
    `;
  }

  /**
   * Render todo list tool using new plan-based renderer
   * @param {number} agentDepth - Depth of the agent (0 = main, 1+ = sub-agent)
   */
  renderTodoList(uniqueId, toolArgs, agentDepth = 0) {
    const todos = JSON.parse(toolArgs)['todos'];

    // Use the new plan-based renderer for a sleeker UI
    // Note: uniqueId is generated in createCollapsibleToolMessage but not needed here
    // as PlanMessageRenderer generates its own unique IDs
    return this.planRenderer.createPlanMessage(
      'Task Plan',
      todos,
      { collapsible: true, showHeader: true, agentDepth }
    );
  }

  /**
   * Render tools that only show file path when expanded (no output)
   * Used for read_file, edit_file, etc.
   * @param {number} agentDepth - Depth of the agent (0 = main, 1+ = sub-agent)
   */
  renderInputOnlyExpandable(uniqueId, toolName, _firstArgument, toolArgs, agentDepth = 0) {
    const icon = ToolIcons.getIcon(toolName);
    const baseName = this._formatToolName(toolName);
    // Add emoji prefix for sub-agent tools
    const displayName = agentDepth > 0 ? `🔹 ${baseName}` : baseName;
    const subagentBadge = this._renderSubagentBadge(agentDepth);

    // Extract the file_path from args (more reliable than firstArgument for edit_file)
    const filePath = this._extractFilePath(toolArgs);
    const displayTarget = filePath ? this._extractFilename(filePath) : '';

    return `
      <div data-llamabot="tool-expandable" data-tool-id="${uniqueId}" data-input-only="true" data-agent-depth="${agentDepth}" onclick="toggleToolExpand('${uniqueId}')">
        <div data-llamabot="tool-compact" data-expandable="true" data-agent-depth="${agentDepth}">
          ${subagentBadge}
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
   * @param {number} agentDepth - Depth of the agent (0 = main, 1+ = sub-agent)
   */
  renderDefaultTool(uniqueId, toolName, firstArgument, toolArgs, toolResult, agentDepth = 0) {
    const icon = ToolIcons.getIcon(toolName);
    const baseName = this._formatToolName(toolName);
    // Add emoji prefix for sub-agent tools
    const displayName = agentDepth > 0 ? `🔹 ${baseName}` : baseName;
    const displayTarget = this._extractDisplayTarget(toolName, firstArgument);
    const isExpandable = EXPANDABLE_TOOLS.includes(toolName);
    const subagentBadge = this._renderSubagentBadge(agentDepth);

    if (isExpandable) {
      // Store the tool data for later retrieval
      this.toolDataStore.set(uniqueId, {
        toolName,
        toolArgs,
        toolResult: toolResult || '',
        agentDepth
      });

      return `
        <div data-llamabot="tool-expandable" data-tool-id="${uniqueId}" data-agent-depth="${agentDepth}" onclick="toggleToolExpand('${uniqueId}')">
          <div data-llamabot="tool-compact" data-expandable="true" data-agent-depth="${agentDepth}">
            ${subagentBadge}
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
      <div data-llamabot="tool-compact" data-agent-depth="${agentDepth}">
        ${subagentBadge}
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
   * Extract display target based on tool type
   * For delegate tools, show truncated task description
   * For file tools, show filename
   * For bash, clean up command
   */
  _extractDisplayTarget(toolName, firstArgument) {
    if (!firstArgument) return '';

    // For delegate tools, truncate the task description
    if (toolName === 'delegate_task' || toolName === 'delegate_research') {
      const maxLength = 60;
      if (firstArgument.length > maxLength) {
        return firstArgument.substring(0, maxLength) + '...';
      }
      return firstArgument;
    }

    // For other tools, use existing filename extraction
    return this._extractFilename(firstArgument);
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
   * Truncate output if too long, preserving beginning and end
   */
  _truncateOutput(output, maxLength = 6000) {
    if (!output) return '(no output)';
    const str = String(output);
    if (str.length > maxLength) {
      // Keep 50% from beginning and 50% from end to preserve both context and results
      const headChars = Math.floor(maxLength * 0.5);
      const tailChars = Math.floor(maxLength * 0.5);
      const removed = str.length - maxLength;
      return str.substring(0, headChars) +
             `\n\n... (${removed} characters truncated) ...\n\n` +
             str.substring(str.length - tailChars);
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
