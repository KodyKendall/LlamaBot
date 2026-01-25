/**
 * WebSocket message routing and processing
 */

export class MessageHandler {
  constructor(appState, streamingState, messageRenderer, iframeManager, scrollManager, tokenIndicator, config) {
    this.appState = appState;
    this.streamingState = streamingState;
    this.messageRenderer = messageRenderer;
    this.iframeManager = iframeManager;
    this.scrollManager = scrollManager;
    this.tokenIndicator = tokenIndicator;
    this.config = config;

    // Track active plan for real-time updates
    this.activePlanId = null;
    this.planStepMapping = new Map(); // Maps step content to step DOM IDs

    // Track current thinking message for inline display
    this.currentThinkingId = null;
    this.currentThinkingBuffer = '';
    // Track if a non-thinking message was added since last thinking
    // Used to determine if we need a new thinking bubble or can append to existing
    this.hasNonThinkingMessageSinceLastThinking = false;
  }

  /**
   * Normalize streaming content from different LLM provider formats
   * Handles OpenAI (string), Anthropic/Claude, and Gemini (array of content blocks) formats
   * @param {string|Array} content - The content from AIMessageChunk
   * @returns {string} - Extracted text content
   */
  normalizeLLMStreamingContent(content) {
    if (!content) return '';

    // OpenAI format: content is a simple string
    if (typeof content === 'string') {
      return content;
    }

    // Anthropic/Claude/Gemini/GPT-5 Codex format: content is array of content blocks
    // Examples:
    //   Anthropic: [{type: "text", text: "Hello"}]
    //   Gemini: [{type: "text", text: "Hello"}, {type: "image_url", image_url: "..."}]
    //   Gemini streaming: [{type: "text_delta", text: "Hello"}]
    //   GPT-5 Codex: [{type: "text", text: "Hello"}, {type: "reasoning", text: "thinking..."}]
    if (Array.isArray(content) && content.length > 0) {
      return content
        .filter(block => {
          if (!block || typeof block !== 'object') return false;

          // Handle text blocks from all providers
          const isTextBlock = block.type === 'text' ||
                             block.type === 'text_delta' ||  // Gemini streaming
                             block.text;
          return isTextBlock;
        })
        .map(block => {
          const text = block.text || block.content || '';
          // Filter out undefined/null values
          return (text !== undefined && text !== null && text !== 'undefined') ? text : '';
        })
        .filter(text => text.length > 0)  // Remove empty strings
        .join('');
    }

    return '';
  }

  /**
   * Extract thinking/reasoning content from LLM response
   * Handles different provider formats:
   * - Claude: {type: "thinking", thinking: "..."}
   * - OpenAI: {type: "reasoning", summary: [...]} or {type: "reasoning_summary", text: "..."}
   * - Gemini: {thought: true, text: "..."}
   * @param {Array|null} thinkingBlocks - The thinking content blocks from the backend
   * @returns {string|null} - Extracted thinking text or null
   */
  extractThinkingContent(thinkingBlocks) {
    if (!thinkingBlocks || !Array.isArray(thinkingBlocks) || thinkingBlocks.length === 0) {
      return null;
    }

    return thinkingBlocks
      .map(block => {
        // Handle different provider formats
        if (block.thinking) return block.thinking;  // Claude format
        if (block.text) return block.text;          // OpenAI/Gemini format
        // OpenAI reasoning format: summary is an array of {type: "summary_text", text: "..."} objects
        if (block.summary && Array.isArray(block.summary)) {
          return block.summary
            .map(s => s.text || s.content || (typeof s === 'string' ? s : ''))
            .filter(t => t)
            .join('\n');
        }
        return '';
      })
      .filter(text => text.length > 0)
      .join('');
  }

  /**
   * Handle incoming WebSocket message
   */
  handleMessage(data) {
    // Update token indicator if token usage data is present
    if (data.token_usage && this.tokenIndicator) {
      this.tokenIndicator.update(data.token_usage);
    }

    if (data.type === 'AIMessageChunk') {
      this.handleAIMessageChunk(data);
    } else if (data.type === 'ai') {
      this.handleAIMessage(data);
    } else {
      this.handleGenericMessage(data);
    }
  }

  /**
   * Handle AI message chunks (streaming)
   */
  handleAIMessageChunk(data) {
    // Handle thinking/reasoning content if present - render inline in message history
    if (data.thinking) {
      const thinkingText = this.extractThinkingContent(data.thinking);
      if (thinkingText) {
        this.handleThinkingContent(thinkingText);
      }
    }

    // Handle regular text content
    // Check if content has actual text (not just empty array or empty string)
    const hasActualContent = data.content && (
      (typeof data.content === 'string' && data.content.length > 0) ||
      (Array.isArray(data.content) && data.content.length > 0)
    );

    if (hasActualContent) {
      // Finalize current thinking block before starting text content
      this.finalizeCurrentThinking();
      // Regular text content streaming
      this.handleTextContent(data);
    } else if (data.base_message?.tool_call_chunks?.length > 0) {
      // Tool call arguments streaming - only finalize if there are actual tool calls
      this.finalizeCurrentThinking();
      this.handleToolCallChunk(data);
    }
  }

  /**
   * Finalize and collapse the current thinking block
   * Called when transitioning from thinking to text/tool content
   */
  finalizeCurrentThinking() {
    if (this.currentThinkingId) {
      this.messageRenderer.collapseThinkingMessage(this.currentThinkingId);
      // Mark that a non-thinking message occurred - next thinking will need new bubble
      this.hasNonThinkingMessageSinceLastThinking = true;
      // Note: We do NOT reset currentThinkingId here anymore
      // We only create a new bubble if hasNonThinkingMessageSinceLastThinking is true
    }
  }

  /**
   * Handle thinking content - render as inline message in history
   * @param {string} thinkingText - The thinking text to append
   */
  handleThinkingContent(thinkingText) {
    // Create new thinking bubble only if:
    // 1. We don't have one yet, OR
    // 2. A non-thinking message was added since the last thinking content
    if (!this.currentThinkingId || this.hasNonThinkingMessageSinceLastThinking) {
      this.currentThinkingId = `thinking-${Date.now()}`;
      this.currentThinkingBuffer = '';
      this.hasNonThinkingMessageSinceLastThinking = false;
    }

    // Append to buffer
    this.currentThinkingBuffer += thinkingText;

    // Render/update the inline thinking message
    this.messageRenderer.renderThinkingMessage(this.currentThinkingBuffer, this.currentThinkingId);
  }

  /**
   * Handle text content streaming
   */
  handleTextContent(data) {
    let currentMessage = this.appState.getCurrentAiMessage();

    // Extract text content using universal parser (handles both OpenAI and Anthropic formats)
    const textContent = this.normalizeLLMStreamingContent(data.content);

    // Only create/update if we have actual content
    if (textContent) {
      // Create content message on first content chunk (or after tool calls)
      if (!currentMessage) {
        const messageElement = this.messageRenderer.addMessage('', 'ai', data);
        messageElement.classList.add('content-message'); // Add class to identify content messages
        this.appState.setCurrentAiMessage(messageElement);
        currentMessage = messageElement;
      }

      this.appState.appendToMessageBuffer(textContent);

      // Update message with parsed markdown
      const parser = this.messageRenderer.markdownParser;
      let fullMessage = this.appState.getMessageBuffer();
      currentMessage.innerHTML = parser.parse(fullMessage);
    }

    // Handle scrolling
    this.scrollManager.checkIfUserAtBottom();
    this.scrollManager.scrollToBottom();
  }

  /**
   * Handle tool call chunks (HTML generation)
   * This is for the STREAMING PREVIEW feature (contentFrame)
   */
  handleToolCallChunk(data) {
    if (!data.base_message?.tool_call_chunks?.[0]) {
      return;
    }

    const toolCallData = data.base_message.tool_call_chunks[0].args;
    this.streamingState.appendData(toolCallData);

    // Check for HTML start
    if (this.streamingState.checkForHtmlStart()) {
      this.handleHtmlStreamStart(data);
    }

    // Check for HTML end
    if (this.streamingState.checkForHtmlEnd()) {
      this.handleHtmlStreamEnd();
    }

    // Schedule iframe update if streaming
    if (this.streamingState.isStreaming()) {
      this.streamingState.scheduleIframeFlush(() => {
        this.iframeManager.flushToStreamingPreview(this.streamingState.getCleanedFullMessage());
      });
      this.streamingState.clearFragmentBuffer();
    }
  }

  /**
   * Handle start of HTML streaming
   * This is for the STREAMING PREVIEW feature (contentFrame)
   */
  handleHtmlStreamStart(data) {
    // Show loading state
    if (!this.appState.getCurrentAiMessage()) {
      const messageElement = this.messageRenderer.addMessage('', 'ai', data);
      this.appState.setCurrentAiMessage(messageElement);
    }

    const currentMessage = this.appState.getCurrentAiMessage();
    currentMessage.innerHTML = '🎨 Generating your page...';

    // Create overlay animation for streaming preview
    this.iframeManager.createStreamingOverlay();
  }

  /**
   * Handle end of HTML streaming
   * This is for the STREAMING PREVIEW feature (contentFrame)
   */
  handleHtmlStreamEnd() {
    // Update AI message
    const currentMessage = this.appState.getCurrentAiMessage();
    if (currentMessage) {
      currentMessage.innerHTML = '✨ Page generated successfully!';
    }

    // Clear pending flush
    this.streamingState.clearIframeFlush();

    // Final flush to streaming preview iframe
    this.iframeManager.flushToStreamingPreview(this.streamingState.getCleanedFullMessage());

    // Remove overlay
    this.iframeManager.removeStreamingOverlay();

    // Reset streaming state
    this.streamingState.reset();
  }

  /**
   * Handle complete AI message
   */
  handleAIMessage(data) {
    // Only process tool calls if present
    if (data.base_message?.tool_calls?.length > 0) {
      // Finalize any current thinking block before tool calls
      this.finalizeCurrentThinking();

      // "Close" the current content message by resetting the buffer and clearing current message
      // This ensures that when streaming resumes, a NEW message bubble is created
      this.appState.setCurrentAiMessage(null);
      this.appState.currentAiMessageBuffer = ''; // Reset buffer for next content chunk

      // Check if there was streamed content (Claude/Gemini) or not (OpenAI)
      const textContent = this.normalizeLLMStreamingContent(data.content);
      const hasContent = textContent && textContent.trim() !== '';

      if (!hasContent) {
        // OpenAI style: Content is empty, create the tool call message with the tool calls
        // This will render as a tool call message (not a content message)
        this.messageRenderer.addMessage('', 'ai', data.base_message);
      } else {
        // Claude/Gemini style: Content was already streamed
        // Just create the tool call placeholders for each tool call
        for (const toolCall of data.base_message.tool_calls) {
          const toolCallMessage = {
            tool_calls: [toolCall]
          };
          // Empty content since this is just the tool call placeholder
          this.messageRenderer.addMessage('', 'ai', toolCallMessage);
        }
      }

      // Track plan if this is a write_todos tool call
      this.trackPlanFromToolCall(data.base_message.tool_calls);
    } else {
      // No tool calls - the message was already streamed via AIMessageChunk
      // Remove empty content message if it has no content
      const currentAiMessage = this.appState.getCurrentAiMessage();
      if (currentAiMessage && currentAiMessage.innerHTML.trim() === '') {
        currentAiMessage.remove();
      }
    }
  }

  /**
   * Track plan creation and build step mapping for real-time updates
   */
  trackPlanFromToolCall(toolCalls) {
    const writeTodosTool = toolCalls.find(tc => tc.name === 'write_todos');
    if (!writeTodosTool) return;

    try {
      const todos = JSON.parse(writeTodosTool.args)?.todos;
      if (!todos || !Array.isArray(todos)) return;

      // Find the plan element that was just created
      setTimeout(() => {
        const planElements = document.querySelectorAll('[data-plan-id]');
        const latestPlan = planElements[planElements.length - 1];

        if (latestPlan) {
          this.activePlanId = latestPlan.getAttribute('data-plan-id');

          // Build mapping of todo content to step IDs
          this.planStepMapping.clear();
          const stepElements = latestPlan.querySelectorAll('[data-step-id]');
          stepElements.forEach((stepEl, index) => {
            if (todos[index]) {
              this.planStepMapping.set(todos[index].content, stepEl.getAttribute('data-step-id'));
            }
          });
        }
      }, 100);
    } catch (error) {
      console.warn('Failed to track plan:', error);
    }
  }

  /**
   * Update plan step status in real-time
   * Called when receiving updated todo list from streaming
   */
  updatePlanSteps(newTodos) {
    if (!this.activePlanId || !this.messageRenderer.toolRenderer?.planRenderer) {
      return;
    }

    const planRenderer = this.messageRenderer.toolRenderer.planRenderer;

    // Use the new updatePlanMessage method which updates the entire plan state
    planRenderer.updatePlanMessage(this.activePlanId, newTodos);
  }

  /**
   * Handle generic messages (tool, error, end, etc.)
   */
  handleGenericMessage(data) {
    if (data.type === 'end') {
      this.messageRenderer.handleEndMessage();
      // Clear plan tracking when conversation ends
      this.activePlanId = null;
      this.planStepMapping.clear();
      // Finalize any remaining thinking message and reset all tracking
      this.finalizeCurrentThinking();
      this.currentThinkingId = null;
      this.currentThinkingBuffer = '';
      this.hasNonThinkingMessageSinceLastThinking = false;
    } else {
      // Finalize thinking before tool messages so they appear interspersed
      if (data.type === 'tool') {
        this.finalizeCurrentThinking();
      }
      this.messageRenderer.addMessage(data.content, data.type, data.base_message);

      // Check if this is an updated todo list and update plan steps in real-time
      if (data.base_message?.name === 'write_todos' && data.base_message?.args) {
        try {
          const argsObj = typeof data.base_message.args === 'string'
            ? JSON.parse(data.base_message.args)
            : data.base_message.args;

          if (argsObj?.todos) {
            this.updatePlanSteps(argsObj.todos);
          }
        } catch (error) {
          console.warn('Failed to parse updated todos:', error);
        }
      }
    }
  }
}
