/**
 * ThinkingDisplay - Real-time display of LLM thinking/reasoning content
 *
 * Displays model thinking/reasoning in a dedicated area during streaming,
 * then collapses to a clickable summary when the response completes.
 *
 * Supports content from multiple providers:
 * - Gemini: thinking_config with include_thoughts
 * - Claude: extended thinking blocks
 * - OpenAI: reasoning effort summaries
 */
export class ThinkingDisplay {
  /**
   * @param {HTMLElement} thinkingAreaElement - The thinking-area DOM element
   * @param {LoadingVerbs} loadingVerbs - The loading verbs instance for cycling messages
   */
  constructor(thinkingAreaElement, loadingVerbs) {
    this.element = thinkingAreaElement;
    this.loadingVerbs = loadingVerbs;
    this.thinkingBuffer = '';
    this.isCollapsed = true;
    this.hasThinkingContent = false;
  }

  /**
   * Append thinking content during streaming
   * @param {string} text - The thinking text to append
   */
  appendThinking(text) {
    if (!text) return;

    this.thinkingBuffer += text;
    this.hasThinkingContent = true;
    this.isCollapsed = false;

    // Stop the loading verb cycling since we have real content
    if (this.loadingVerbs) {
      this.loadingVerbs.stopCycling();
    }

    this.render();
  }

  /**
   * Render the thinking display in expanded (streaming) mode
   */
  render() {
    if (!this.element) return;

    // Escape HTML to prevent XSS
    const escapedText = this.escapeHtml(this.thinkingBuffer);

    this.element.innerHTML = `
      <div class="thinking-content expanded">
        <div class="thinking-header">
          <span class="thinking-icon">🧠</span>
          <span class="thinking-label">Thinking...</span>
        </div>
        <div class="thinking-text">${escapedText}</div>
      </div>
    `;
    this.element.classList.remove('hidden');

    // Auto-scroll the thinking text to bottom
    const textEl = this.element.querySelector('.thinking-text');
    if (textEl) {
      textEl.scrollTop = textEl.scrollHeight;
    }
  }

  /**
   * Collapse the thinking display after streaming completes
   * Shows a clickable summary that can be expanded
   */
  collapse() {
    if (!this.element) return;

    // If no thinking content was received, just hide the area
    if (!this.hasThinkingContent) {
      this.clear();
      return;
    }

    this.isCollapsed = true;

    // Calculate summary (first 100 chars or first line)
    const summary = this.getSummary();
    const escapedSummary = this.escapeHtml(summary);
    const escapedFull = this.escapeHtml(this.thinkingBuffer);

    this.element.innerHTML = `
      <div class="thinking-content collapsed" data-expanded="false">
        <div class="thinking-header clickable" onclick="this.parentElement.dataset.expanded = this.parentElement.dataset.expanded === 'true' ? 'false' : 'true'">
          <span class="thinking-icon">🧠</span>
          <span class="thinking-label">Thought process</span>
          <span class="thinking-toggle">▶</span>
        </div>
        <div class="thinking-summary">${escapedSummary}...</div>
        <div class="thinking-full">${escapedFull}</div>
      </div>
    `;

    // Keep visible but in collapsed state
    this.element.classList.remove('hidden');
  }

  /**
   * Get a summary of the thinking content for collapsed view
   * @returns {string} - First 100 characters or first line
   */
  getSummary() {
    if (!this.thinkingBuffer) return '';

    // Get first line or first 100 chars, whichever is shorter
    const firstLine = this.thinkingBuffer.split('\n')[0];
    const maxLength = 100;

    if (firstLine.length <= maxLength) {
      return firstLine;
    }

    return this.thinkingBuffer.substring(0, maxLength);
  }

  /**
   * Clear the thinking display and reset state
   * Called when starting a new message
   */
  clear() {
    this.thinkingBuffer = '';
    this.hasThinkingContent = false;
    this.isCollapsed = true;

    if (this.element) {
      this.element.classList.add('hidden');
      this.element.innerHTML = '';
    }
  }

  /**
   * Reset for a new message (clears buffer but doesn't hide yet)
   * Called before streaming starts
   */
  reset() {
    this.thinkingBuffer = '';
    this.hasThinkingContent = false;
    this.isCollapsed = true;
  }

  /**
   * Escape HTML special characters to prevent XSS
   * @param {string} text - Raw text
   * @returns {string} - Escaped text safe for innerHTML
   */
  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }
}
