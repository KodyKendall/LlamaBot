/**
 * Tool message rendering with collapsible UI
 */

import { generateUniqueId } from '../utils/domHelpers.js';

export class ToolMessageRenderer {
  constructor(iframeManager = null, getRailsDebugInfoCallback = null) {
    this.iframeManager = iframeManager;
    this.getRailsDebugInfoCallback = getRailsDebugInfoCallback;
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
   * Render todo list tool
   */
  renderTodoList(uniqueId, toolArgs) {
    let todos = JSON.parse(toolArgs)['todos'];

    // Sort todos: in_progress → pending → completed
    const statusOrder = { 'in_progress': 0, 'pending': 1, 'completed': 2 };
    todos.sort((a, b) => statusOrder[a.status] - statusOrder[b.status]);

    let todoListHTML = '';
    let currentStatus = null;

    todos.forEach((todo, index) => {
      // Add section header if status changed
      if (todo.status !== currentStatus) {
        currentStatus = todo.status;
        const sectionTitle = todo.status === 'in_progress' ? 'In Progress' :
                            todo.status === 'pending' ? 'Pending' : 'Completed';
        todoListHTML += `<div class="todo-section-header">${sectionTitle}</div>`;
      }

      const todoId = `todo-${uniqueId}-${index}`;
      const statusClass = `todo-status-${todo.status.replace('_', '-')}`;
      const icon = todo.status === 'completed' ? '✅' :
                   todo.status === 'in_progress' ? '🎯' : '🕒';

      todoListHTML += `
        <div class="todo-item ${statusClass}" onclick="toggleTodo('${todoId}')">
          <span class="todo-icon">${icon}</span>
          <span class="todo-text" id="${todoId}">${todo.content}</span>
        </div>
      `;
    });

    return `
      <div class="tool-collapsible" onclick="toggleToolCollapsible('${uniqueId}')">
        <span class="tool-summary">🎯 Todo List (${todos.length} tasks)</span>
      </div>
      <div class="tool-content" id="${uniqueId}">
        <div class="todo-container">
          ${todoListHTML}
        </div>
      </div>
    `;
  }

  /**
   * Render edit file tool
   */
  renderEditFile(uniqueId, firstArgument, toolArgs, toolResult) {
    return `
      <div class="tool-collapsible" onclick="toggleToolCollapsible('${uniqueId}')">
        <span class="tool-summary">Edit ${firstArgument}</span>
      </div>
      <div class="tool-content" id="${uniqueId}">
        <div class="tool-details">
          <strong>Arguments:</strong><br>
          <pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolArgs}</pre>
          ${toolResult ? `<strong>Result:</strong><br><pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolResult}</pre>` : ''}
        </div>
      </div>
    `;
  }

  /**
   * Render default tool
   */
  renderDefaultTool(uniqueId, toolName, firstArgument, toolArgs, toolResult) {
    return `
      <div class="tool-collapsible" onclick="toggleToolCollapsible('${uniqueId}')">
        <span class="tool-summary">🔨${toolName} ${firstArgument}</span>
      </div>
      <div class="tool-content" id="${uniqueId}">
        <div class="tool-details">
          <strong>Arguments:</strong><br>
          <pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolArgs}</pre>
          ${toolResult ? `<strong>Result:</strong><br><pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolResult}</pre>` : ''}
        </div>
      </div>
    `;
  }

  /**
   * Update existing tool message with result
   */
  updateCollapsibleToolMessage(messageDiv, toolResult, baseMessage) {
    const toolContent = messageDiv.querySelector('.tool-content .tool-details');

    // Special handling for edit_file
    if (baseMessage.name === 'edit_file') {
      const toolHeaderLabel = messageDiv.querySelector('.tool-collapsible');
      if (baseMessage.artifact?.status === 'success') {
        toolHeaderLabel.innerHTML = toolHeaderLabel.innerHTML.replace('Edit', '✅ Edit');

        // Refresh the main iframe when edit_file succeeds
        // This allows the user to see their code changes in real-time
        if (this.iframeManager && this.getRailsDebugInfoCallback) {
          this.iframeManager.refreshRailsApp(this.getRailsDebugInfoCallback);
        }
      } else if (baseMessage.artifact?.status === 'error') {
        toolHeaderLabel.innerHTML = toolHeaderLabel.innerHTML.replace('Edit', '❌ Edit');
      }
    }

    if (toolContent) {
      // Check if result already exists to avoid duplication
      if (!toolContent.innerHTML.includes('<strong>Result:</strong>')) {
        toolContent.innerHTML += `<br><strong>Result:</strong><br><pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolResult}</pre>`;
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
