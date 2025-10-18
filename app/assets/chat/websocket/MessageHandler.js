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
    }

    const currentMessage = this.appState.getCurrentAiMessage();

    // Remove typing indicator if present
    if (currentMessage.querySelector('.typing-indicator')) {
      currentMessage.innerHTML = '';
    }

    // Handle different LLM formats
    if (this.appState.getAgentConfig().type === 'claude_llm_model') {
      if (data.content && data.content.length > 0) {
        this.appState.appendToMessageBuffer(data.content[0].text);
      }
    } else {
      this.appState.appendToMessageBuffer(data.content);
    }

    // Update message with parsed markdown
    const parser = this.messageRenderer.markdownParser;
    currentMessage.innerHTML = parser.parse(this.appState.getMessageBuffer());

    // Handle scrolling
    this.scrollManager.checkIfUserAtBottom();
    this.scrollManager.scrollToBottom();
  }

  /**
   * Handle tool call chunks (HTML generation)
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
        this.iframeManager.flushToIframe(this.streamingState.getCleanedFullMessage());
      });
      this.streamingState.clearFragmentBuffer();
    }
  }

  /**
   * Handle start of HTML streaming
   */
  handleHtmlStreamStart(data) {
    // Show loading state
    if (!this.appState.getCurrentAiMessage()) {
      const messageElement = this.messageRenderer.addMessage('', 'ai', data);
      this.appState.setCurrentAiMessage(messageElement);
    }

    const currentMessage = this.appState.getCurrentAiMessage();
    currentMessage.innerHTML = '🎨 Generating your page...';

    // Create overlay animation
    this.iframeManager.createStreamingOverlay();
  }

  /**
   * Handle end of HTML streaming
   */
  handleHtmlStreamEnd() {
    // Update AI message
    const currentMessage = this.appState.getCurrentAiMessage();
    if (currentMessage) {
      currentMessage.innerHTML = '✨ Page generated successfully!';
    }

    // Clear pending flush
    this.streamingState.clearIframeFlush();

    // Final flush
    this.iframeManager.flushToIframe(this.streamingState.getCleanedFullMessage());

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
    if (data.base_message?.tool_calls?.length > 0) {
      this.messageRenderer.addMessage(data.content, data.type, data.base_message);
    }
  }

  /**
   * Handle generic messages (tool, error, end, etc.)
   */
  handleGenericMessage(data) {
    this.messageRenderer.addMessage(data.content, data.type, data.base_message);
  }
}
