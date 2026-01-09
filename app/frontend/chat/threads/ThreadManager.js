/**
 * Thread/conversation management - fetch, load, switch threads
 */

export class ThreadManager {
  constructor(messageRenderer, menuManager, scrollManager) {
    this.messageRenderer = messageRenderer;
    this.menuManager = menuManager;
    this.scrollManager = scrollManager;
    // Pagination state
    this.nextCursor = null;
    this.hasMore = false;

    // Expose on window for inline onclick handlers
    window.threadManager = this;
  }

  /**
   * Fetch threads from server with cursor-based pagination
   * @param {string|null} cursor - Optional cursor for pagination
   */
  async fetchThreads(cursor = null) {
    try {
      this.showThreadsLoading();

      let url = '/threads?limit=3';
      if (cursor) {
        url += `&before=${encodeURIComponent(cursor)}`;
      }

      const response = await fetch(url);

      // Check if response is OK
      if (!response.ok) {
        console.warn(`Thread fetch returned ${response.status}: ${response.statusText}`);
        this.showThreadsError(`Server returned ${response.status}`);
        return;
      }

      // Check if response is JSON
      const contentType = response.headers.get('content-type');
      if (!contentType || !contentType.includes('application/json')) {
        console.warn('Thread fetch did not return JSON, got:', contentType);
        this.showThreadsError('Invalid response format');
        return;
      }

      const data = await response.json();
      const { threads, next_cursor, has_more } = data;

      // Store pagination state
      this.nextCursor = next_cursor;
      this.hasMore = has_more;

      // Populate menu (threads already sorted by backend)
      this.populateMenuWithThreads(threads, has_more);
    } catch (error) {
      console.error('Error fetching threads:', error);
      this.showThreadsError(error.message);
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
  showThreadsError(errorMsg = 'Failed to load') {
    const menuItems = document.querySelector('.menu-items');
    if (!menuItems) return;

    menuItems.innerHTML = `
      <div class="menu-item" style="opacity: 0.6; cursor: default; color: #ff6b6b;">
        <small>⚠️ ${errorMsg}</small>
      </div>
      <div class="menu-item" style="opacity: 0.8; cursor: default;">
        <small style="color: #888;">Threads unavailable - chat still works!</small>
        Failed to load conversations
      </div>
      <div class="menu-item" onclick="window.threadManager.fetchThreads()" style="color: var(--accent-color); cursor: pointer;">
        🔄 Retry
      </div>
    `;
  }

  /**
   * Populate menu with thread items
   * @param {Array} threads - Array of thread metadata objects (lightweight format from /threads API)
   * @param {boolean} hasMore - Whether there are more threads to load
   */
  populateMenuWithThreads(threads, hasMore = false) {
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
      // Use pre-computed title from metadata (fast - no state extraction needed)
      // Falls back to extracting from state for backwards compatibility
      let title = thread.title;
      if (!title && thread.state) {
        // Backwards compatibility: extract from state if metadata title not available
        const messages = thread.state[0]?.messages || [];
        const summary = this.generateConversationSummary(messages);
        title = summary.title;
      }
      title = title || 'New Conversation';

      const menuItem = document.createElement('div');
      menuItem.className = 'menu-item';
      menuItem.textContent = title;

      menuItem.onclick = () => this.handleThreadClick(thread.thread_id, title);

      menuItems.appendChild(menuItem);
    });

    // Add "Load More" button if there are more threads
    if (hasMore) {
      const loadMoreItem = document.createElement('div');
      loadMoreItem.className = 'menu-item load-more';
      loadMoreItem.innerHTML = '<i class="fa-solid fa-ellipsis"></i> Load more...';
      loadMoreItem.style.color = 'var(--accent-color)';
      loadMoreItem.style.cursor = 'pointer';
      loadMoreItem.onclick = () => this.loadMoreThreads();
      menuItems.appendChild(loadMoreItem);
    }
  }

  /**
   * Load more threads using the stored cursor
   */
  async loadMoreThreads() {
    if (!this.nextCursor || !this.hasMore) return;

    // Show loading state on the Load More button
    const loadMoreBtn = document.querySelector('.menu-item.load-more');
    if (loadMoreBtn) {
      loadMoreBtn.innerHTML = '<div class="typing-indicator">Loading...</div>';
    }

    try {
      const url = `/threads?limit=3&before=${encodeURIComponent(this.nextCursor)}`;
      const response = await fetch(url);
      const data = await response.json();
      const { threads, next_cursor, has_more } = data;

      // Update pagination state
      this.nextCursor = next_cursor;
      this.hasMore = has_more;

      // Append new threads to existing menu
      this.appendThreadsToMenu(threads, has_more);
    } catch (error) {
      console.error('Error loading more threads:', error);
      // Restore the Load More button on error
      if (loadMoreBtn) {
        loadMoreBtn.innerHTML = '<i class="fa-solid fa-ellipsis"></i> Load more...';
      }
    }
  }

  /**
   * Append threads to the existing menu (for pagination)
   * @param {Array} threads - Array of thread metadata objects to append
   * @param {boolean} hasMore - Whether there are more threads to load
   */
  appendThreadsToMenu(threads, hasMore) {
    const menuItems = document.querySelector('.menu-items');
    if (!menuItems) return;

    // Remove existing "Load More" button
    const existingLoadMore = menuItems.querySelector('.load-more');
    if (existingLoadMore) {
      existingLoadMore.remove();
    }

    // Append new thread items using lightweight metadata
    threads.forEach(thread => {
      // Use pre-computed title from metadata (fast - no state extraction needed)
      let title = thread.title;
      if (!title && thread.state) {
        // Backwards compatibility: extract from state if metadata title not available
        const messages = thread.state[0]?.messages || [];
        const summary = this.generateConversationSummary(messages);
        title = summary.title;
      }
      title = title || 'New Conversation';

      const menuItem = document.createElement('div');
      menuItem.className = 'menu-item';
      menuItem.textContent = title;
      menuItem.onclick = () => this.handleThreadClick(thread.thread_id, title);
      menuItems.appendChild(menuItem);
    });

    // Add new "Load More" button if there are more
    if (hasMore) {
      const loadMoreItem = document.createElement('div');
      loadMoreItem.className = 'menu-item load-more';
      loadMoreItem.innerHTML = '<i class="fa-solid fa-ellipsis"></i> Load more...';
      loadMoreItem.style.color = 'var(--accent-color)';
      loadMoreItem.style.cursor = 'pointer';
      loadMoreItem.onclick = () => this.loadMoreThreads();
      menuItems.appendChild(loadMoreItem);
    }
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

  /**
   * Create a new thread - clears messages and generates new thread ID
   */
  createNewThread() {
    // Clear current messages
    this.messageRenderer.clearMessages();

    // Show default welcome message
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

    // Generate new thread ID and dispatch event to update app state
    const newThreadId = this.generateThreadId();
    window.dispatchEvent(new CustomEvent('threadChanged', {
      detail: { threadId: newThreadId }
    }));

    // Close menu
    if (this.menuManager) {
      this.menuManager.closeMenu();
    }

    // Scroll to bottom
    if (this.scrollManager) {
      this.scrollManager.scrollToBottom(true);
    }
  }

  /**
   * Generate a new thread ID
   */
  generateThreadId() {
    return `${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
  }
}
