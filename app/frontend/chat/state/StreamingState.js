/**
 * HTML streaming state management
 */

import { CONFIG } from '../config.js';

export class StreamingState {
  constructor() {
    this.reset();
  }

  /**
   * Reset all streaming state
   */
  reset() {
    this.htmlFragmentBuffer = '';
    this.fullMessageBuffer = '';
    this.htmlChunksStartedStreaming = false;
    this.htmlChunksEndedStreaming = false;
    this.iframeFlushTimer = null;
  }

  /**
   * Append data to buffers
   */
  appendData(data) {
    this.htmlFragmentBuffer += data;
    this.fullMessageBuffer += data;
  }

  /**
   * Check if HTML streaming has started
   */
  checkForHtmlStart() {
    const htmlTagIndex = this.htmlFragmentBuffer.indexOf('<html');
    if (htmlTagIndex !== -1 && !this.htmlChunksStartedStreaming) {
      this.htmlChunksStartedStreaming = true;
      this.htmlFragmentBuffer = this.htmlFragmentBuffer.substring(htmlTagIndex);
      return true;
    }
    return false;
  }

  /**
   * Check if HTML streaming has ended
   */
  checkForHtmlEnd() {
    const endingHtmlTagIndex = this.fullMessageBuffer.indexOf('</html>');
    if (endingHtmlTagIndex !== -1 && !this.htmlChunksEndedStreaming) {
      this.htmlChunksEndedStreaming = true;
      return true;
    }
    return false;
  }

  /**
   * Get cleaned fragment for iframe update
   */
  getCleanedFragment() {
    return this.htmlFragmentBuffer
      .replace(/\\n/g, '\n')
      .replace(/\\"/g, '"')
      .replace(/\\t/g, '\t');
  }

  /**
   * Get full cleaned message
   */
  getCleanedFullMessage() {
    return this.fullMessageBuffer
      .replace(/\\n/g, '\n')
      .replace(/\\"/g, '"')
      .replace(/\\t/g, '\t')
      .replace(/\\r/g, '\r');
  }

  /**
   * Clear fragment buffer after flushing
   */
  clearFragmentBuffer() {
    this.htmlFragmentBuffer = '';
  }

  /**
   * Schedule iframe flush
   */
  scheduleIframeFlush(callback) {
    if (!this.iframeFlushTimer) {
      this.iframeFlushTimer = setTimeout(() => {
        callback();
        this.iframeFlushTimer = null;
      }, CONFIG.IFRAME_REFRESH_MS);
    }
  }

  /**
   * Clear pending iframe flush
   */
  clearIframeFlush() {
    if (this.iframeFlushTimer) {
      clearTimeout(this.iframeFlushTimer);
      this.iframeFlushTimer = null;
    }
  }

  /**
   * Check if streaming is active
   */
  isStreaming() {
    return this.htmlChunksStartedStreaming && !this.htmlChunksEndedStreaming;
  }

  /**
   * Check if streaming has started
   */
  hasStarted() {
    return this.htmlChunksStartedStreaming;
  }

  /**
   * Check if streaming has ended
   */
  hasEnded() {
    return this.htmlChunksEndedStreaming;
  }
}
