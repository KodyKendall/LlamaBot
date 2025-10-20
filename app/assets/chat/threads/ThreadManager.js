/**
 * Thread/conversation management - fetch, load, switch threads
 */

export class ThreadManager {
  constructor(messageRenderer, menuManager, scrollManager) {
    this.messageRenderer = messageRenderer;
    this.menuManager = menuManager;
    this.scrollManager = scrollManager;
  }

  /**
   * Fetch all threads from server
   */
  async fetchThreads() {
    try {
      this.showThreadsLoading();

      const response = await fetch('/threads');
      const threads = await response.json();
      console.log(threads);

      // Sort by date, newest first
      threads.sort((a, b) => new Date(b.state[4]) - new Date(a.state[4]));

      // Populate menu
      this.populateMenuWithThreads(threads);
    } catch (error) {
      console.error('Error fetching threads:', error);
      this.showThreadsError();
    }
  }

  /**
   * Show loading state in threads menu
   */
  showThreadsLoading() {
    const menuItems = document.querySelector('.menu-items');
    if (!menuItems) return;

    // Show loading with random verb
    const loadingVerbs = [
      "Frolicking", "Dreaming", "Cyborging", "Llama'ing", "Grazing", "Thinking",
      "Working", "Cogitating", "Circuiting", "Llaminating", "Synapsing", "Daydreaming"
    ];
    const randomVerb = loadingVerbs[Math.floor(Math.random() * loadingVerbs.length)];

    menuItems.innerHTML = `
      <div class="menu-item" style="opacity: 0.6; cursor: default;">
        <div class="typing-indicator" style="justify-content: center;">🦙 ${randomVerb}...</div>
      </div>
    `;
  }

  /**
   * Show error state in threads menu
   */
  showThreadsError() {
    const menuItems = document.querySelector('.menu-items');
    if (!menuItems) return;

    menuItems.innerHTML = `
      <div class="menu-item" style="opacity: 0.6; cursor: default; color: #ff6b6b;">
        Failed to load conversations
      </div>
      <div class="menu-item" onclick="window.threadManager.fetchThreads()" style="color: var(--accent-color); cursor: pointer;">
        🔄 Retry
      </div>
    `;
  }

  /**
   * Populate menu with thread items
   */
  populateMenuWithThreads(threads) {
    const menuItems = document.querySelector('.menu-items');
    if (!menuItems) return;

    menuItems.innerHTML = '';

    if (!threads || threads.length === 0) {
      const emptyItem = document.createElement('div');
      emptyItem.className = 'menu-item';
      emptyItem.style.opacity = '0.6';
      emptyItem.textContent = 'No conversations yet';
      menuItems.appendChild(emptyItem);
      return;
    }

    threads.forEach(thread => {
      const messages = thread.state[0]?.messages || [];
      const { title } = this.generateConversationSummary(messages);

      const menuItem = document.createElement('div');
      menuItem.className = 'menu-item';
      menuItem.textContent = title;

      menuItem.onclick = () => this.handleThreadClick(thread.thread_id, title);

      menuItems.appendChild(menuItem);
    });
  }

  /**
   * Generate conversation summary/title
   */
  generateConversationSummary(messages) {
    if (!messages || messages.length === 0) {
      return { title: 'New Conversation' };
    }

    const firstUserMessage = messages.find(msg => msg.type === 'human');
    let title = 'New Conversation';

    if (firstUserMessage && firstUserMessage.content) {
      // Extract text content in case it's an array/object
      const textContent = this.normalizeHistoricalMessageContent(firstUserMessage.content);
      title = textContent.substring(0, 50);
      if (textContent.length > 50) {
        title += '...';
      }
    }

    return { title };
  }

  /**
   * Handle thread click - load thread messages
   */
  async handleThreadClick(threadId, title) {
    console.log(`Loading thread: ${threadId} - ${title}`);

    try {
      // Emit event for app state to update thread ID
      window.dispatchEvent(new CustomEvent('threadChanged', {
        detail: { threadId }
      }));

      // Fetch thread messages
      const response = await fetch(`/chat-history/${threadId}`);
      const threadData = await response.json();

      // Load messages
      this.loadThreadMessages(threadData);

      // Close menu
      if (this.menuManager) {
        this.menuManager.closeMenu();
      }
    } catch (error) {
      console.error('Error loading thread:', error);
      alert('Failed to load conversation. Please try again.');
    }
  }

  /**
   * Load thread messages into chat
   */
  loadThreadMessages(threadData) {
    // Clear current messages
    this.messageRenderer.clearMessages();

    // Get messages from thread
    const messages = threadData[0]?.messages || [];

    if (messages.length === 0) {
      // Show default message
      const defaultMessage = document.createElement('div');
      defaultMessage.className = 'message ai-message';
      defaultMessage.textContent = "Hi! I'm Leonardo. What are we building today?";

      const messageHistory = this.messageRenderer.getMessageHistory();
      const scrollButton = document.getElementById('scrollToBottomBtn');

      if (scrollButton) {
        messageHistory.insertBefore(defaultMessage, scrollButton);
      } else {
        messageHistory.appendChild(defaultMessage);
      }

      return;
    }

    // Render all messages
    messages.forEach(message => {
      // Extract text content from message.content
      const textContent = this.normalizeHistoricalMessageContent(message.content);
      this.messageRenderer.addMessage(textContent, message.type, message);
    });

    // Scroll to bottom
    if (this.scrollManager) {
      this.scrollManager.scrollToBottom(true);
    }
  }

  /**
   * Normalize historical LangGraph message content for display
   * Handles string, array (multimodal), and object content structures from stored messages
   * @param {string|Array|Object} content - The content from a stored LangGraph message
   * @returns {string} - Extracted text content ready for display
   */
  normalizeHistoricalMessageContent(content) {
    // If content is already a string, return it
    if (typeof content === 'string') {
      return content;
    }

    // If content is an array (multimodal message), extract text parts
    if (Array.isArray(content)) {
      return content
        .map(block => {
          if (typeof block === 'string') {
            return block;
          }
          if (block && typeof block === 'object') {
            // Handle different content block types
            if (block.type === 'text' && block.text) {
              return block.text;
            }
            if (block.text) {
              return block.text;
            }
          }
          return '';
        })
        .filter(text => text.length > 0)
        .join('\n');
    }

    // If content is an object, try to extract text
    if (content && typeof content === 'object') {
      if (content.text) {
        return content.text;
      }
      if (content.content) {
        return this.normalizeHistoricalMessageContent(content.content);
      }
      // Fallback: stringify the object
      return JSON.stringify(content);
    }

    // Fallback for null/undefined
    return '';
  }
}
