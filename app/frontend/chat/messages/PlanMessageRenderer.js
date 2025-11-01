/**
 * Modern minimalist plan rendering for 2025 AI UX
 * - Only shows active task with shimmer
 * - Hides completed tasks in subtle collapsed view
 * - Clean, unobtrusive design
 */

import { generateUniqueId } from '../utils/domHelpers.js';

export class PlanMessageRenderer {
  constructor(iframeManager = null, getRailsDebugInfoCallback = null) {
    this.iframeManager = iframeManager;
    this.getRailsDebugInfoCallback = getRailsDebugInfoCallback;
  }

  /**
   * Create modern minimalist plan message
   * Shows only active task prominently, hides the rest
   */
  createPlanMessage(planTitle, steps, options = {}) {
    const uniqueId = generateUniqueId('plan');

    const inProgressTasks = steps.filter(s => s.status === 'in_progress');
    const pendingTasks = steps.filter(s => s.status === 'pending');
    const completedTasks = steps.filter(s => s.status === 'completed');

    // Main active task display (only show if there's something in progress)
    const activeTaskHTML = inProgressTasks.length > 0
      ? this._renderActiveTask(inProgressTasks[0], uniqueId)
      : '';

    // Subtle progress indicator
    const progressHTML = this._renderProgressIndicator(completedTasks.length, steps.length, uniqueId);

    // Hidden completed tasks (expandable)
    const completedHTML = completedTasks.length > 0
      ? this._renderCompletedTasks(completedTasks, uniqueId)
      : '';

    return `
      <div class="plan-modern" data-plan-id="${uniqueId}">
        ${activeTaskHTML}
        ${progressHTML}
        ${completedHTML}
      </div>
    `;
  }

  /**
   * Render the active task with prominent shimmer effect
   */
  _renderActiveTask(task, planId) {
    const taskId = `active-${planId}`;
    const displayText = task.activeForm || task.content;

    return `
      <div class="plan-active-task" data-task-id="${taskId}">
        <div class="plan-shimmer-container">
          <div class="plan-shimmer-bar"></div>
        </div>
        <div class="plan-active-content">
          <div class="plan-spinner-dot"></div>
          <span class="plan-active-text">${this._escapeHtml(displayText)}</span>
        </div>
      </div>
    `;
  }

  /**
   * Render minimal progress indicator
   */
  _renderProgressIndicator(completed, total, planId) {
    if (total === 0) return '';

    const percentage = Math.round((completed / total) * 100);
    const allDone = completed === total;

    return `
      <div class="plan-progress-bar" data-progress-id="${planId}">
        <div class="plan-progress-fill" style="width: ${percentage}%"></div>
      </div>
      <div class="plan-progress-text" onclick="togglePlanDetails('${planId}')">
        ${allDone
          ? `<span class="plan-done-badge">✓ Complete</span>`
          : `<span class="plan-count">${completed}/${total} tasks</span>`
        }
        <svg class="plan-expand-arrow" viewBox="0 0 16 16" fill="none">
          <path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
      </div>
    `;
  }

  /**
   * Render collapsed completed tasks list
   */
  _renderCompletedTasks(tasks, planId) {
    const tasksHTML = tasks.map(task => `
      <div class="plan-completed-item">
        <div class="plan-check-tiny">✓</div>
        <span>${this._escapeHtml(task.content)}</span>
      </div>
    `).join('');

    return `
      <div class="plan-completed-list" id="plan-details-${planId}" style="display: none;">
        <div class="plan-completed-header">Completed</div>
        ${tasksHTML}
      </div>
    `;
  }

  /**
   * Update plan with new task states
   * This handles real-time updates as tasks progress
   */
  updatePlanMessage(planId, newSteps) {
    const planElement = document.querySelector(`[data-plan-id="${planId}"]`);
    if (!planElement) return;

    const inProgressTasks = newSteps.filter(s => s.status === 'in_progress');
    const completedTasks = newSteps.filter(s => s.status === 'completed');

    // Update active task
    const activeTaskElement = planElement.querySelector('.plan-active-task');
    if (inProgressTasks.length > 0) {
      if (activeTaskElement) {
        // Update existing active task
        const textElement = activeTaskElement.querySelector('.plan-active-text');
        if (textElement) {
          textElement.textContent = inProgressTasks[0].activeForm || inProgressTasks[0].content;
        }
      } else {
        // Add new active task
        const activeHTML = this._renderActiveTask(inProgressTasks[0], planId);
        planElement.insertAdjacentHTML('afterbegin', activeHTML);
      }
    } else if (activeTaskElement) {
      // Remove active task with fade out
      activeTaskElement.style.opacity = '0';
      setTimeout(() => activeTaskElement.remove(), 300);
    }

    // Update progress bar
    const progressFill = planElement.querySelector('.plan-progress-fill');
    const progressText = planElement.querySelector('.plan-progress-text');
    const percentage = Math.round((completedTasks.length / newSteps.length) * 100);

    if (progressFill) {
      progressFill.style.width = `${percentage}%`;
    }

    if (progressText) {
      const allDone = completedTasks.length === newSteps.length;
      const arrowIcon = `<svg class="plan-expand-arrow" viewBox="0 0 16 16" fill="none"><path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
      progressText.innerHTML = allDone
        ? `<span class="plan-done-badge">✓ Complete</span>${arrowIcon}`
        : `<span class="plan-count">${completedTasks.length}/${newSteps.length} tasks</span>${arrowIcon}`;
    }

    // Update completed list
    const completedList = planElement.querySelector('.plan-completed-list');
    if (completedTasks.length > 0) {
      const completedHTML = completedTasks.map(task => `
        <div class="plan-completed-item">
          <div class="plan-check-tiny">✓</div>
          <span>${this._escapeHtml(task.content)}</span>
        </div>
      `).join('');

      if (completedList) {
        completedList.innerHTML = `
          <div class="plan-completed-header">Completed</div>
          ${completedHTML}
        `;
      } else {
        const newCompletedHTML = `
          <div class="plan-completed-list" id="plan-details-${planId}" style="display: none;">
            <div class="plan-completed-header">Completed</div>
            ${completedHTML}
          </div>
        `;
        planElement.insertAdjacentHTML('beforeend', newCompletedHTML);
      }
    }
  }

  /**
   * Legacy method for backward compatibility
   */
  updatePlanStep(planId, stepId, newStatus, options = {}) {
    // This method is kept for backward compatibility but not actively used
    // The new approach uses updatePlanMessage() which updates the entire plan state
    console.warn('updatePlanStep is deprecated, use updatePlanMessage instead');
  }

  /**
   * Escape HTML to prevent XSS
   */
  _escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}

/**
 * Global toggle function for plan details
 */
window.togglePlanDetails = function(planId) {
  const detailsElement = document.getElementById(`plan-details-${planId}`);
  const planElement = document.querySelector(`[data-plan-id="${planId}"]`);
  const progressText = planElement?.querySelector('.plan-progress-text');

  if (detailsElement) {
    const isVisible = detailsElement.style.display !== 'none';
    detailsElement.style.display = isVisible ? 'none' : 'block';

    // Toggle expanded class on progress text for arrow rotation
    if (progressText) {
      progressText.classList.toggle('expanded', !isVisible);
    }

    // Animate in
    if (!isVisible) {
      detailsElement.style.opacity = '0';
      setTimeout(() => {
        detailsElement.style.opacity = '1';
      }, 10);
    }
  }
};
