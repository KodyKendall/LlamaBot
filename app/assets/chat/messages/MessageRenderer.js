/**
 * Message rendering and display logic
 */

import { MarkdownParser } from './MarkdownParser.js';
import { ToolMessageRenderer } from './ToolMessageRenderer.js';

export class MessageRenderer {
  constructor(messageHistoryElement) {
    this.messageHistory = messageHistoryElement;
    this.markdownParser = new MarkdownParser();
    this.toolRenderer = new ToolMessageRenderer();
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
    messageDiv.innerHTML = this.markdownParser.parse(content);

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
   */
  handleEndMessage() {
    console.log('end of stream');

    // Hide thinking indicator
    const thinkingIndicator = document.getElementById('thinkingIndicator');
    if (thinkingIndicator) {
      thinkingIndicator.classList.add('hidden');
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
