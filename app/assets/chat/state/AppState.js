/**
 * Centralized application state management
 */

import { CONFIG } from '../config.js';

export class AppState {
  constructor() {
    // WebSocket connection
    this.socket = null;

    // Thread management
    this.currentThreadId = null;

    // Message streaming
    this.currentAiMessage = null;
    this.currentAiMessageBuffer = '';
    this.thinkingMessage = null;

    // Agent configuration (mutable for dynamic selection)
    this.agentConfig = {
      name: CONFIG.AGENT.NAME,
      type: CONFIG.AGENT.TYPE
    };
  }

  /**
   * Set the WebSocket connection
   */
  setSocket(socket) {
    this.socket = socket;
  }

  /**
   * Get the WebSocket connection
   */
  getSocket() {
    return this.socket;
  }

  /**
   * Set current thread ID
   */
  setThreadId(threadId) {
    this.currentThreadId = threadId;
  }

  /**
   * Get current thread ID
   */
  getThreadId() {
    return this.currentThreadId;
  }

  /**
   * Generate a new thread ID if one doesn't exist
   */
  ensureThreadId() {
    if (!this.currentThreadId) {
      this.currentThreadId = crypto.randomUUID();
    }
    return this.currentThreadId;
  }

  /**
   * Set current AI message element
   */
  setCurrentAiMessage(element) {
    this.currentAiMessage = element;
  }

  /**
   * Get current AI message element
   */
  getCurrentAiMessage() {
    return this.currentAiMessage;
  }

  /**
   * Set thinking message element
   */
  setThinkingMessage(element) {
    this.thinkingMessage = element;
  }

  /**
   * Get thinking message element
   */
  getThinkingMessage() {
    return this.thinkingMessage;
  }

  /**
   * Append to AI message buffer
   */
  appendToMessageBuffer(content) {
    this.currentAiMessageBuffer += content;
  }

  /**
   * Get AI message buffer
   */
  getMessageBuffer() {
    return this.currentAiMessageBuffer;
  }

  /**
   * Reset message state for new message
   */
  resetMessageState() {
    this.currentAiMessage = null;
    this.currentAiMessageBuffer = '';
    this.thinkingMessage = null;
  }

  /**
   * Update agent configuration based on mode
   */
  setAgentMode(mode) {
    const agentName = CONFIG.AGENT_MODES[mode];
    if (agentName) {
      this.agentConfig.name = agentName;
    }
  }

  /**
   * Get current agent configuration
   */
  getAgentConfig() {
    return this.agentConfig;
  }
}
