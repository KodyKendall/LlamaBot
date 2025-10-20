/**
 * Message rendering and display logic
 */

import { MarkdownParser } from './MarkdownParser.js';
import { ToolMessageRenderer } from './ToolMessageRenderer.js';

export class MessageRenderer {
  constructor(messageHistoryElement, iframeManager = null, getRailsDebugInfoCallback = null, scrollManager = null, loadingVerbs = null) {
    this.messageHistory = messageHistoryElement;
    this.markdownParser = new MarkdownParser();
    this.toolRenderer = new ToolMessageRenderer(iframeManager, getRailsDebugInfoCallback);
    this.scrollManager = scrollManager;
    this.loadingVerbs = loadingVerbs;
  }

  /**
   * Add a message to the conversation window
   * @param {string} content - The content of the message
   * @param {string} type - The type of message ('human', 'ai', 'tool', 'error', 'end')
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
    messageDiv.className = 'message user-message';
    messageDiv.textContent = content;
    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Render AI message
   */
  renderAiMessage(content, baseMessage) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message ai-message';

    // Ensure content is a valid string (handle undefined, null, etc.)
    const safeContent = (content !== undefined && content !== null && content !== 'undefined')
      ? String(content)
      : '';

    messageDiv.innerHTML = this.markdownParser.parse(safeContent);

    // Check if this is a tool call message (OpenAI format)
    if ((content === '' || content === null) && baseMessage?.tool_calls?.length > 0) {
      messageDiv.className = 'message tool-message';
      const toolCall = baseMessage.tool_calls[0];
      let firstArgument = toolCall.args[Object.keys(toolCall.args)[0]] || '';

      messageDiv.innerHTML = this.toolRenderer.createCollapsibleToolMessage(
        toolCall.name,
        firstArgument,
        JSON.stringify(toolCall.args),
        ''
      );
      messageDiv.id = baseMessage.tool_calls[0].id;
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
      messageDiv.className = 'message tool-message';
      this.toolRenderer.updateCollapsibleToolMessage(messageDiv, content, baseMessage);
      return messageDiv;
    }

    // TODO: Handle Claude's LLM model case where message div doesn't exist yet
    return null;
  }

  /**
   * Render error message
   */
  renderErrorMessage(content) {
    const messageDiv = document.createElement('div');
    messageDiv.className = 'message error-message';
    messageDiv.textContent = content;
    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Handle end of stream
   * @param {object} appState - Application state to access current AI message
   */
  handleEndMessage(appState = null) {
    console.log('end of stream');

    // Stop cycling verbs
    if (this.loadingVerbs) {
      this.loadingVerbs.stopCycling();
    }

    // Hide thinking indicator in input area
    const thinkingIndicator = document.getElementById('thinkingIndicator');
    if (thinkingIndicator) {
      thinkingIndicator.classList.add('hidden');
      thinkingIndicator.textContent = '';
    }

    // Remove typing indicator from AI message div if present
    if (appState) {
      const currentAiMessage = appState.getCurrentAiMessage();
      if (currentAiMessage) {
        const typingIndicator = currentAiMessage.querySelector('.typing-indicator');
        if (typingIndicator) {
          typingIndicator.remove();
        }
      }
    }

    // Play task completed sound
    const taskCompletedSound = document.getElementById('taskCompletedSound');
    if (taskCompletedSound) {
      taskCompletedSound.play().catch(error => {
        console.log('Could not play sound:', error);
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
    const scrollButton = document.getElementById('scrollToBottomBtn');

    if (scrollButton) {
      this.messageHistory.insertBefore(messageDiv, scrollButton);
    } else {
      this.messageHistory.appendChild(messageDiv);
    }

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
    const toolMessages = allMessages.filter(msg => msg.classList.contains('tool-message'));

    if (toolMessages.length === 0) return;

    // Find the last tool message
    const lastToolMessage = toolMessages[toolMessages.length - 1];

    // Move AI message after the last tool message (if it's not already there)
    const aiMessageIndex = allMessages.indexOf(aiMessageDiv);
    const lastToolIndex = allMessages.indexOf(lastToolMessage);

    if (aiMessageIndex < lastToolIndex) {
      // Remove and reinsert after last tool message
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
