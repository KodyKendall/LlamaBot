/**
 * Message rendering and display logic
 */

import { MarkdownParser } from './MarkdownParser.js';
import { ToolMessageRenderer } from './ToolMessageRenderer.js';

export class MessageRenderer {
  constructor(messageHistoryElement, iframeManager = null, getRailsDebugInfoCallback = null, scrollManager = null, loadingVerbs = null, config = {}, container = null, elements = {}, faviconBadgeManager = null) {
    this.messageHistory = messageHistoryElement;
    this.markdownParser = new MarkdownParser();
    this.toolRenderer = new ToolMessageRenderer(iframeManager, getRailsDebugInfoCallback);
    this.scrollManager = scrollManager;
    this.loadingVerbs = loadingVerbs;
    this.config = config;
    this.container = container;
    this.elements = elements;
    this.faviconBadgeManager = faviconBadgeManager;
  }

  /**
   * Add a message to the conversation window
   * @param {string} content - The content of the message
   * @param {string} type - The type of message ('human', 'ai', 'tool', 'error', 'queued', 'end')
   * @param {object} baseMessage - The base langgraph message object
   * @param {Array} attachments - Optional array of attachment metadata {filename, mime_type}
   * @returns {HTMLElement|null} The message div or null
   */
  addMessage(content, type, baseMessage = null, attachments = null) {
    if (type === 'human') {
      return this.renderHumanMessage(content, attachments);
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
   * Render human message with markdown support, copy button, and attachment badges
   * @param {string} content - Text content of the message
   * @param {Array} attachments - Optional array of attachment metadata {filename, mime_type}
   */
  renderHumanMessage(content, attachments = []) {
    const messageDiv = document.createElement('div');
    messageDiv.setAttribute('data-llamabot', 'human-message');

    // Store raw content for copy functionality
    messageDiv.setAttribute('data-raw-content', content);

    // Parse markdown (same as AI messages)
    messageDiv.innerHTML = this.markdownParser.parse(content);

    // Apply custom CSS classes if configured
    if (this.config.cssClasses?.humanMessage) {
      messageDiv.className = this.config.cssClasses.humanMessage;
    }

    // Add attachment badges if present
    if (attachments && attachments.length > 0) {
      const attachmentContainer = this.createAttachmentBadges(attachments);
      messageDiv.appendChild(attachmentContainer);
    }

    // Add copy button for human messages with content
    if (content && content.trim()) {
      this.addCopyButton(messageDiv);
    }

    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Create attachment badges for display in message history
   * @param {Array} attachments - Array of attachment objects with filename, mime_type
   * @returns {HTMLElement} - Container with attachment badges
   */
  createAttachmentBadges(attachments) {
    const container = document.createElement('div');
    container.className = 'message-attachments';

    // Icon mapping for different file types
    const iconMap = {
      'application/pdf': 'fa-file-pdf',
      'image/png': 'fa-file-image',
      'image/jpeg': 'fa-file-image',
      'image/gif': 'fa-file-image',
      'image/webp': 'fa-file-image',
      'video/webm': 'fa-file-video',
      'video/mp4': 'fa-file-video',
      'audio/mpeg': 'fa-file-audio',
      'audio/wav': 'fa-file-audio',
    };

    attachments.forEach(attachment => {
      const badge = document.createElement('div');
      badge.className = 'message-attachment-badge';

      const icon = iconMap[attachment.mime_type] || 'fa-file';
      const filename = attachment.filename || 'attachment';

      badge.innerHTML = `
        <i class="fa-solid ${icon}"></i>
        <span class="attachment-filename" title="${filename}">${this.truncateFilename(filename)}</span>
      `;

      container.appendChild(badge);
    });

    return container;
  }

  /**
   * Truncate filename for display while preserving extension
   * @param {string} filename - The filename to truncate
   * @param {number} maxLength - Maximum length before truncation
   * @returns {string} - Truncated filename
   */
  truncateFilename(filename, maxLength = 20) {
    if (filename.length <= maxLength) return filename;
    const ext = filename.split('.').pop();
    const name = filename.slice(0, -(ext.length + 1));
    const truncatedName = name.slice(0, maxLength - ext.length - 4) + '...';
    return `${truncatedName}.${ext}`;
  }

  /**
   * Render AI message with copy button
   */
  renderAiMessage(content, baseMessage) {
    const messageDiv = document.createElement('div');
    messageDiv.setAttribute('data-llamabot', 'ai-message');

    // Ensure content is a valid string (handle undefined, null, etc.)
    const safeContent = (content !== undefined && content !== null && content !== 'undefined')
      ? String(content)
      : '';

    // Store raw markdown for copy functionality
    messageDiv.setAttribute('data-raw-content', safeContent);

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

      // Add copy button for regular AI messages (not tool messages)
      if (safeContent) {
        this.addCopyButton(messageDiv);
      }
    }

    this.insertMessage(messageDiv);
    return messageDiv;
  }

  /**
   * Add a copy button to a message element
   * @param {HTMLElement} messageDiv - The message element to add the button to
   */
  addCopyButton(messageDiv) {
    const copyBtn = document.createElement('button');
    copyBtn.setAttribute('data-llamabot', 'copy-btn');
    copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i>';
    copyBtn.title = 'Copy raw markdown';
    copyBtn.onclick = (e) => {
      e.stopPropagation();
      const rawContent = messageDiv.getAttribute('data-raw-content');
      navigator.clipboard.writeText(rawContent).then(() => {
        copyBtn.innerHTML = '<i class="fa-solid fa-check"></i>';
        setTimeout(() => {
          copyBtn.innerHTML = '<i class="fa-regular fa-copy"></i>';
        }, 1500);
      });
    };
    messageDiv.appendChild(copyBtn);
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

    // Show error badge on favicon (red)
    if (this.faviconBadgeManager) {
      this.faviconBadgeManager.showError();
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

    // Stop favicon thinking indicator (as safety net - showError/showComplete also stop it)
    if (this.faviconBadgeManager) {
      this.faviconBadgeManager.stopThinking();
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

    // Finalize all AI messages (add copy buttons to those that don't have them)
    this.finalizeAiMessages();

    // Play task completed sound
    const taskCompletedSound = document.getElementById('taskCompletedSound');
    if (taskCompletedSound) {
      taskCompletedSound.play().catch(() => {
        // Sound playback failed (likely due to autoplay restrictions)
      });
    }

    // Show completion badge on favicon (green)
    if (this.faviconBadgeManager) {
      this.faviconBadgeManager.showComplete();
    }

    // Emit custom event for other components to handle
    window.dispatchEvent(new CustomEvent('streamEnded'));

    return null;
  }

  /**
   * Finalize all AI messages by adding copy buttons to those that don't have them
   * Called when streaming ends to add copy buttons to streamed messages
   */
  finalizeAiMessages() {
    const aiMessages = this.messageHistory.querySelectorAll('[data-llamabot="ai-message"]');
    aiMessages.forEach(messageDiv => {
      // Skip if already has a copy button
      if (messageDiv.querySelector('[data-llamabot="copy-btn"]')) {
        return;
      }

      // Skip if no content
      const rawContent = messageDiv.getAttribute('data-raw-content');
      if (!rawContent || rawContent.trim() === '') {
        return;
      }

      // Add copy button
      this.addCopyButton(messageDiv);
    });
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
