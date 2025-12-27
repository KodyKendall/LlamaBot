/**
 * Token Context Indicator - Shows context window usage as a circular progress wheel
 */

export class TokenIndicator {
  constructor() {
    this.element = document.querySelector('[data-llamabot="token-indicator"]');
    this.wheel = null;
    this.percentageEl = null;
    this.countEl = null;

    // Context window limit (80K = summarization threshold)
    this.maxTokens = 80000;
    this.currentTokens = 0;

    this.init();
  }

  /**
   * Initialize the token indicator
   */
  init() {
    if (!this.element) {
      console.warn('TokenIndicator: Element not found');
      return;
    }

    this.wheel = this.element.querySelector('.token-wheel-progress');
    this.percentageEl = this.element.querySelector('.token-percentage');
    this.countEl = this.element.querySelector('.token-count');
  }

  /**
   * Update the indicator with new token usage data
   * @param {Object} tokenUsage - Token usage data from LangChain
   * @param {number} tokenUsage.input_tokens - Input tokens used
   * @param {number} tokenUsage.output_tokens - Output tokens used
   * @param {number} tokenUsage.total_tokens - Total tokens used
   */
  update(tokenUsage) {
    if (!this.element || !tokenUsage) return;

    // Add input_tokens from this message to running total
    // input_tokens = context sent to the model (includes conversation history)
    // output_tokens = tokens generated in the response
    // We track input_tokens as it represents the actual context window usage
    const inputTokens = tokenUsage.input_tokens || 0;
    const outputTokens = tokenUsage.output_tokens || 0;

    // The input_tokens already includes all prior context, so we use it directly
    // as our current context size, plus the new output tokens
    if (inputTokens > 0) {
      this.currentTokens = inputTokens + outputTokens;
    } else if (tokenUsage.total_tokens) {
      // Fallback: if only total is provided, use it as-is (might be cumulative)
      this.currentTokens = tokenUsage.total_tokens;
    }

    this.render();
  }

  /**
   * Set the current token count directly (e.g., when loading a thread)
   * @param {number} tokens - Total tokens used
   */
  setTokenCount(tokens) {
    this.currentTokens = tokens;
    this.render();
  }

  /**
   * Reset the token count (e.g., when starting a new thread)
   */
  reset() {
    this.currentTokens = 0;
    this.render();
  }

  /**
   * Render the indicator UI
   */
  render() {
    if (!this.element) return;

    const percentage = Math.min((this.currentTokens / this.maxTokens) * 100, 100);

    // Update SVG circle stroke-dasharray
    // The circumference is set up so that 100 = full circle
    if (this.wheel) {
      this.wheel.setAttribute('stroke-dasharray', `${percentage} 100`);
    }

    // Update percentage text
    if (this.percentageEl) {
      this.percentageEl.textContent = `${Math.round(percentage)}%`;
    }

    // Update tooltip token count
    if (this.countEl) {
      this.countEl.textContent = this.formatTokenCount(this.currentTokens);
    }

    // Update color classes based on usage level
    this.element.classList.remove('warning', 'critical');
    if (percentage > 85) {
      this.element.classList.add('critical');
    } else if (percentage > 60) {
      this.element.classList.add('warning');
    }
  }

  /**
   * Format token count for display
   * @param {number} count - Raw token count
   * @returns {string} Formatted count (e.g., "45.2K")
   */
  formatTokenCount(count) {
    if (count >= 1000) {
      return `${(count / 1000).toFixed(1)}K`;
    }
    return count.toString();
  }

  /**
   * Get current token count
   * @returns {number}
   */
  getTokenCount() {
    return this.currentTokens;
  }

  /**
   * Get percentage of context used
   * @returns {number}
   */
  getPercentage() {
    return Math.min((this.currentTokens / this.maxTokens) * 100, 100);
  }
}
