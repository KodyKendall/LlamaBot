/**
 * WebSocket connection management with auto-reconnection
 */

import { getWebSocketUrl, getRailsUrl } from '../config.js';

export class WebSocketManager {
  constructor(messageHandler, config = {}, elements = {}) {
    this.messageHandler = messageHandler;
    this.config = config;
    this.elements = elements;
    this.socket = null;
    this.reconnectTimer = null;
  }

  /**
   * Initialize WebSocket connection
   */
  connect() {
    const wsUrl = this.config.websocketUrl || getWebSocketUrl();
    this.socket = new WebSocket(wsUrl);

    this.socket.onopen = () => this.handleOpen();
    this.socket.onclose = () => this.handleClose();
    this.socket.onerror = (error) => this.handleError(error);
    this.socket.onmessage = (event) => this.handleMessage(event);

    // Set initial iframe src for HTTPS
    if (window.location.protocol === 'https:' && this.elements.liveSiteFrame) {
      this.elements.liveSiteFrame.src = getRailsUrl();
    }

    return this.socket;
  }

  /**
   * Handle WebSocket open event
   */
  handleOpen() {
    console.log('WebSocket connected');
    this.updateConnectionStatus(true);

    if (this.elements.sendButton) {
      this.elements.sendButton.disabled = false;
    }

    // Emit custom event
    window.dispatchEvent(new CustomEvent('websocketConnected'));
  }

  /**
   * Handle WebSocket close event
   */
  handleClose() {
    console.log('WebSocket disconnected');
    this.updateConnectionStatus(false);

    if (this.elements.sendButton) {
      this.elements.sendButton.disabled = true;
    }

    // Emit custom event
    window.dispatchEvent(new CustomEvent('websocketDisconnected'));

    // Attempt to reconnect after delay
    this.scheduleReconnect();
  }

  /**
   * Handle WebSocket error event
   */
  handleError(error) {
    console.error('WebSocket error:', error);

    // Emit custom event with error
    window.dispatchEvent(new CustomEvent('websocketError', { detail: error }));

    // Call custom error callback if provided
    if (this.config.onError) {
      this.config.onError(error);
    }
  }

  /**
   * Handle incoming WebSocket message
   */
  handleMessage(event) {
    const data = JSON.parse(event.data);
    // console.log('Received:', data.type);
    // console.log('Data:', data);

    // Delegate to message handler
    if (this.messageHandler) {
      this.messageHandler.handleMessage(data);
    }
  }

  /**
   * Send message via WebSocket
   */
  send(data) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(data));
      return true;
    }
    console.error('WebSocket is not connected');
    return false;
  }

  /**
   * Update connection status UI
   */
  updateConnectionStatus(connected) {
    if (!this.elements.connectionStatus) return;

    if (connected) {
      this.elements.connectionStatus.className = 'connection-status connected';
      this.elements.connectionStatus.innerHTML = '<span class="status-dot"></span>';
    } else {
      this.elements.connectionStatus.className = 'connection-status disconnected';
      this.elements.connectionStatus.innerHTML = '<span class="status-dot"></span>';
    }
  }

  /**
   * Schedule reconnection attempt
   */
  scheduleReconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }

    this.reconnectTimer = setTimeout(() => {
      console.log('Attempting to reconnect...');
      this.connect();
    }, this.config.reconnectDelay || 3000);
  }

  /**
   * Get current socket
   */
  getSocket() {
    return this.socket;
  }

  /**
   * Disconnect WebSocket
   */
  disconnect() {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
  }
}
