/**
 * Centralized application state management
 */

import { DEFAULT_CONFIG } from '../config.js';

export class AppState {
  constructor() {
    // WebSocket connection
    this.socket = null;

    // Thread management
    this.currentThreadId = null;

    // Message streaming
    this.currentAiMessage = null;
    this.currentAiMessageBuffer = '';

    // Agent configuration (mutable for dynamic selection)
    this.agentConfig = {
      name: DEFAULT_CONFIG.agent.name,
      type: DEFAULT_CONFIG.agent.type
    };

    // Task duration timer state
    this.taskStartTime = null;
    this.taskTimerInterval = null;

    // Sub-agent depth tracking
    this.currentAgentDepth = 0;
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
  }

  /**
   * Update agent configuration based on mode
   */
  setAgentMode(mode) {
    const agentName = DEFAULT_CONFIG.agentModes[mode];
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

  // ==========================================
  // Task Duration Timer Methods
  // ==========================================

  /**
   * Start the task duration timer
   */
  startTaskTimer() {
    this.taskStartTime = Date.now();
  }

  /**
   * Stop and reset the task duration timer
   */
  stopTaskTimer() {
    this.taskStartTime = null;
    if (this.taskTimerInterval) {
      clearInterval(this.taskTimerInterval);
      this.taskTimerInterval = null;
    }
  }

  /**
   * Get elapsed time in milliseconds since task started
   */
  getElapsedTime() {
    if (!this.taskStartTime) return 0;
    return Date.now() - this.taskStartTime;
  }

  /**
   * Get formatted elapsed time as MM:SS
   */
  getFormattedElapsedTime() {
    if (!this.taskStartTime) return '00:00';
    const elapsed = Date.now() - this.taskStartTime;
    const seconds = Math.floor(elapsed / 1000) % 60;
    const minutes = Math.floor(elapsed / 60000);
    return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
  }

  /**
   * Check if timer is currently running
   */
  isTimerRunning() {
    return this.taskStartTime !== null;
  }

  // ==========================================
  // Agent Depth Tracking Methods
  // ==========================================

  /**
   * Update the current agent depth
   */
  setAgentDepth(depth) {
    this.currentAgentDepth = depth;
  }

  /**
   * Get the current agent depth
   */
  getAgentDepth() {
    return this.currentAgentDepth;
  }

  /**
   * Reset depth tracking (called when task ends)
   */
  resetDepthTracking() {
    this.currentAgentDepth = 0;
  }
}
