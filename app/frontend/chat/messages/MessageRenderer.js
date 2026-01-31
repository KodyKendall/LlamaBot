/**
 * Message rendering and display logic
 */

import { MarkdownParser } from './MarkdownParser.js';
import { ToolMessageRenderer } from './ToolMessageRenderer.js';

export class MessageRenderer {
  constructor(messageHistoryElement, iframeManager = null, getRailsDebugInfoCallback = null, scrollManager = null, loadingVerbs = null, config = {}, container = null, elements = {}) {
    this.messageHistory = messageHistoryElement;
    this.markdownParser = new MarkdownParser();
    this.toolRenderer = new ToolMessageRenderer(iframeManager, getRailsDebugInfoCallback);
    this.scrollManager = scrollManager;
    this.loadingVerbs = loadingVerbs;
    this.config = config;
    this.container = container;
    this.elements = elements;
  }

  /**
   * Add a message to the conversation window
   * @param {string} content - The content of the message
   * @param {string} type - The type of message ('human', 'ai', 'tool', 'error', 'queued', 'end')
   * @param {object} baseMessage - The base langgraph message object
   * @returns {HTMLElement|null} The message div or null
   */
  addMessage(content, type, baseMessage = null) {
    if (type === 'human') {
      return this.renderHumanMessage(content);
    }

    if (type === 'ai') {
      return this.renderAiMessage(content, baseMessage);
    }

    if (type === 'tool') {
      return this.updateToolMessage(content, baseMessage);
    }

    if (type === 'error') {
      return this.renderErrorMessage(content);
    }

    if (type === 'queued') {
      return this.renderQueuedMessage(content);
    }

    if (type === 'end') {
      return this.handleEndMessage();
    }

    return null;
  }

  /**
   * Render human message
   */
  renderHumanMessage(content) {
    const messageDiv = document.createElement('div');
    messageDiv.setAttribute('data-llamabot', 'human-message');
    messageDiv.textContent = content;

    // Apply custom CSS classes if configured
    if (this.config.cssClasses?.humanMessage) {
      messageDiv.className = this.config.cssClasses.humanMessage;
    }

    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Render AI message
   */
  renderAiMessage(content, baseMessage) {
    const messageDiv = document.createElement('div');
    messageDiv.setAttribute('data-llamabot', 'ai-message');

    // Ensure content is a valid string (handle undefined, null, etc.)
    const safeContent = (content !== undefined && content !== null && content !== 'undefined')
      ? String(content)
      : '';

    messageDiv.innerHTML = this.markdownParser.parse(safeContent);

    // Check if this is a tool call message (OpenAI format)
    if ((content === '' || content === null) && baseMessage?.tool_calls?.length > 0) {
      messageDiv.setAttribute('data-llamabot', 'tool-message');
      const toolCall = baseMessage.tool_calls[0];
      let firstArgument = toolCall.args[Object.keys(toolCall.args)[0]] || '';

      messageDiv.innerHTML = this.toolRenderer.createCollapsibleToolMessage(
        toolCall.name,
        firstArgument,
        JSON.stringify(toolCall.args),
        ''
      );
      messageDiv.id = baseMessage.tool_calls[0].id;
    } else {
      // Apply custom CSS classes if configured (only for regular AI messages, not tool messages)
      if (this.config.cssClasses?.aiMessage) {
        messageDiv.className = this.config.cssClasses.aiMessage;
      }
    }

    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Update tool message with result
   */
  updateToolMessage(content, baseMessage) {
    const messageDiv = document.getElementById(baseMessage.tool_call_id);

    if (messageDiv) {
      messageDiv.setAttribute('data-llamabot', 'tool-message');
      this.toolRenderer.updateCollapsibleToolMessage(messageDiv, content, baseMessage);
      return messageDiv;
    }

    // TODO: Handle Claude's LLM model case where message div doesn't exist yet
    return null;
  }

  /**
   * Render or update inline thinking message
   * Creates a collapsible thinking block that persists in the message history
   * @param {string} thinkingText - The thinking content to display
   * @param {string} thinkingId - Unique ID for this thinking block (to allow updates)
   * @returns {HTMLElement} The thinking message div
   */
  renderThinkingMessage(thinkingText, thinkingId = null) {
    const id = thinkingId || `thinking-${Date.now()}`;
    let messageDiv = document.getElementById(id);

    if (!messageDiv) {
      // Create new thinking message - starts collapsed
      messageDiv = document.createElement('div');
      messageDiv.id = id;
      messageDiv.setAttribute('data-llamabot', 'thinking-message');
      messageDiv.className = 'thinking-message';
      messageDiv.innerHTML = this.createThinkingMessageHTML(thinkingText, true);
      this.insertMessage(messageDiv);
    } else {
      // Update existing thinking message
      const textEl = messageDiv.querySelector('.thinking-message-text');
      if (textEl) {
        textEl.textContent = thinkingText;
        // Auto-scroll the thinking text
        textEl.scrollTop = textEl.scrollHeight;
      }
    }

    return messageDiv;
  }

  /**
   * Create HTML for thinking message
   * @param {string} text - The thinking text
   * @param {boolean} collapsed - Whether to start collapsed
   * @returns {string} HTML string
   */
  createThinkingMessageHTML(text, collapsed = false, isStreaming = true) {
    const escapedText = this.escapeHtml(text);
    const expandedClass = collapsed ? '' : 'expanded';
    const streamingClass = isStreaming ? 'streaming' : '';

    return `
      <div class="thinking-message-content ${expandedClass} ${streamingClass}">
        <div class="thinking-message-header" onclick="this.parentElement.classList.toggle('expanded');">
          <span class="thinking-label">thinking...</span>
        </div>
        <div class="thinking-message-text">${escapedText}</div>
      </div>
    `;
  }

  /**
   * Collapse a thinking message (called when streaming ends)
   * Removes expanded class and stops shimmer animation
   * @param {string} thinkingId - The ID of the thinking message to collapse
   */
  collapseThinkingMessage(thinkingId) {
    const messageDiv = document.getElementById(thinkingId);
    if (messageDiv) {
      const content = messageDiv.querySelector('.thinking-message-content');
      if (content) {
        content.classList.remove('expanded');
        content.classList.remove('streaming');
      }
    }
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
   * Render error message
   */
  renderErrorMessage(content) {
    const messageDiv = document.createElement('div');
    messageDiv.setAttribute('data-llamabot', 'error-message');
    messageDiv.textContent = content;

    // Apply custom CSS classes if configured
    if (this.config.cssClasses?.errorMessage) {
      messageDiv.className = this.config.cssClasses.errorMessage;
    }

    this.insertMessage(messageDiv);

    // Stop the thinking indicator when an error occurs
    this.stopThinking();

    // Play error sound
    const taskErrorSound = document.getElementById('taskErrorSound');
    if (taskErrorSound) {
      taskErrorSound.play().catch(() => {
        // Sound playback failed (likely due to autoplay restrictions)
      });
    }

    return messageDiv;
  }

  /**
   * Stop the thinking indicator and restore input state
   */
  stopThinking() {
    // Stop cycling verbs
    if (this.loadingVerbs) {
      this.loadingVerbs.stopCycling();
    }

    // Hide thinking area in input area - use scoped elements if available
    const thinkingArea = this.elements?.thinkingArea || document.getElementById('thinkingArea');
    if (thinkingArea) {
      thinkingArea.classList.add('hidden');
      thinkingArea.innerHTML = '';
    }

    // Restore original placeholder text - use scoped elements if available
    const messageInput = this.elements?.messageInput || document.getElementById('messageInput');
    if (messageInput) {
      messageInput.placeholder = 'Ask Leonardo...';
    }
  }

  /**
   * Render queued message
   */
  renderQueuedMessage(content) {
    const messageDiv = document.createElement('div');
    messageDiv.setAttribute('data-llamabot', 'queued-message');
    messageDiv.textContent = content;

    // Apply custom CSS classes if configured
    if (this.config.cssClasses?.queuedMessage) {
      messageDiv.className = this.config.cssClasses.queuedMessage;
    }

    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Handle end of stream
   */
  handleEndMessage() {
    // Stop the thinking indicator
    this.stopThinking();

    // Play task completed sound
    const taskCompletedSound = document.getElementById('taskCompletedSound');
    if (taskCompletedSound) {
      taskCompletedSound.play().catch(() => {
        // Sound playback failed (likely due to autoplay restrictions)
      });
    }

    // Emit custom event for other components to handle
    window.dispatchEvent(new CustomEvent('streamEnded'));

    return null;
  }

  /**
   * Insert message into the message history
   */
  insertMessage(messageDiv) {
    // Simply append to the end of message history
    // (scroll button is now in input-area, not message-history)
    this.messageHistory.appendChild(messageDiv);

    // Increment unread count if user is not at bottom
    if (this.scrollManager) {
      this.scrollManager.incrementUnreadCount();
    }

    // Auto-scroll if user is already at bottom
    // Use requestAnimationFrame to ensure DOM has updated
    if (this.scrollManager) {
      requestAnimationFrame(() => {
        this.scrollManager.scrollToBottom();
      });
    }
  }

  /**
   * Reposition AI message to be below all tool messages
   * This keeps the streaming AI response at the bottom during tool calls
   */
  repositionAiMessageBelowTools(aiMessageDiv) {
    if (!aiMessageDiv || !aiMessageDiv.parentNode) return;

    // Find all tool messages
    const allMessages = Array.from(this.messageHistory.children);
    const toolMessages = allMessages.filter(msg => msg.getAttribute('data-llamabot') === 'tool-message');

    // If no tool messages, the AI message should stay where it is
    if (toolMessages.length === 0) return;

    // Find the last tool message
    const lastToolMessage = toolMessages[toolMessages.length - 1];

    // Move AI message after the last tool message (if it's not already there)
    const aiMessageIndex = allMessages.indexOf(aiMessageDiv);
    const lastToolIndex = allMessages.indexOf(lastToolMessage);

    // Only reposition if AI message is BEFORE the last tool message
    if (aiMessageIndex < lastToolIndex) {
      // Find thinking message (should stay at bottom)
      const thinkingMessage = allMessages.find(msg => msg.getAttribute('data-llamabot') === 'thinking-message');

      // Insert after last tool message but before thinking message
      if (thinkingMessage) {
        this.messageHistory.insertBefore(aiMessageDiv, thinkingMessage);
      } else {
        const scrollButton = document.getElementById('scrollToBottomBtn');
        if (scrollButton && lastToolMessage.nextSibling === scrollButton) {
          // Insert before scroll button
          this.messageHistory.insertBefore(aiMessageDiv, scrollButton);
        } else if (lastToolMessage.nextSibling) {
          // Insert after last tool message
          this.messageHistory.insertBefore(aiMessageDiv, lastToolMessage.nextSibling);
        } else {
          // Append to end
          this.messageHistory.appendChild(aiMessageDiv);
        }
      }

      // Auto-scroll if user is already at bottom
      if (this.scrollManager) {
        requestAnimationFrame(() => {
          this.scrollManager.scrollToBottom();
        });
      }
    }
  }

  /**
   * Clear all messages
   */
  clearMessages() {
    const scrollButton = document.getElementById('scrollToBottomBtn');
    this.messageHistory.innerHTML = '';

    if (scrollButton) {
      this.messageHistory.appendChild(scrollButton);
    }
  }

  /**
   * Get message history element
   */
  getMessageHistory() {
    return this.messageHistory;
  }
}
