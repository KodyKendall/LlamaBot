/**
 * WebSocket message routing and processing
 */

import { CONFIG } from '../config.js';

export class MessageHandler {
  constructor(appState, streamingState, messageRenderer, iframeManager, scrollManager) {
    this.appState = appState;
    this.streamingState = streamingState;
    this.messageRenderer = messageRenderer;
    this.iframeManager = iframeManager;
    this.scrollManager = scrollManager;
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
   * Handle incoming WebSocket message
   */
  handleMessage(data) {
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
    // debugger;
    if (data.content) {
      // Regular text content streaming
      this.handleTextContent(data);
    } else if (data.content === '' || data.content === null) {
      // Tool call arguments streaming
      this.handleToolCallChunk(data);
    }
  }

  /**
   * Handle text content streaming
   */
  handleTextContent(data) {
    // Initialize AI message if not exists
    if (!this.appState.getCurrentAiMessage()) {
      const messageElement = this.messageRenderer.addMessage('', 'ai', data);
      this.appState.setCurrentAiMessage(messageElement);

      // Add typing indicator initially
      messageElement.innerHTML = '<div class="typing-indicator"><span></span><span></span><span></span></div>';
    }

    const currentMessage = this.appState.getCurrentAiMessage();

    // Extract text content using universal parser (handles both OpenAI and Anthropic formats)
    const textContent = this.normalizeLLMStreamingContent(data.content);

    // Only update if we have actual content
    if (textContent) {
      // Remove typing indicator if present (on first content chunk)
      if (currentMessage.querySelector('.typing-indicator')) {
        currentMessage.innerHTML = '';
      }

      this.appState.appendToMessageBuffer(textContent);

      // Update message with parsed markdown
      const parser = this.messageRenderer.markdownParser;
      let fullMessage = this.appState.getMessageBuffer();
      currentMessage.innerHTML = parser.parse(fullMessage);

      // Reposition AI message to be below all tool messages
      this.messageRenderer.repositionAiMessageBelowTools(currentMessage);
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
    // Only add message if there are tool calls
    // (streaming already displayed the text content via AIMessageChunk)
    if (data.base_message?.tool_calls?.length > 0) {
      // Extract text content (handles all model formats including GPT-5 Codex)
      const textContent = this.normalizeLLMStreamingContent(data.content);
      this.messageRenderer.addMessage(textContent, data.type, data.base_message);
    } else {
      // No tool calls - the message was already streamed via AIMessageChunk
      // Just reset state so next message starts fresh
      this.appState.resetMessageState();
    }
  }

  /**
   * Handle generic messages (tool, error, end, etc.)
   */
  handleGenericMessage(data) {
    // Pass appState to handleEndMessage for typing indicator cleanup
    if (data.type === 'end') {
      this.messageRenderer.handleEndMessage(this.appState);
    } else {
      this.messageRenderer.addMessage(data.content, data.type, data.base_message);
    }
  }
}
