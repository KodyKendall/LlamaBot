/**
 * WebSocket connection management with auto-reconnection
 */

import { getWebSocketUrl, getRailsUrl, CONFIG } from '../config.js';

export class WebSocketManager {
  constructor(messageHandler) {
    this.messageHandler = messageHandler;
    this.socket = null;
    this.reconnectTimer = null;
  }

  /**
   * Initialize WebSocket connection
   */
  connect() {
    const wsUrl = getWebSocketUrl();
    this.socket = new WebSocket(wsUrl);

    this.socket.onopen = () => this.handleOpen();
    this.socket.onclose = () => this.handleClose();
    this.socket.onerror = (error) => this.handleError(error);
    this.socket.onmessage = (event) => this.handleMessage(event);

    // Set initial iframe src for HTTPS
    if (window.location.protocol === 'https:') {
      const liveSiteFrame = document.getElementById('liveSiteFrame');
      if (liveSiteFrame) {
        liveSiteFrame.src = getRailsUrl();
      }
    }

    return this.socket;
  }

  /**
   * Handle WebSocket open event
   */
  handleOpen() {
    console.log('WebSocket connected');
    this.updateConnectionStatus(true);

    const sendButton = document.getElementById('sendButton');
    if (sendButton) {
      sendButton.disabled = false;
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

    const sendButton = document.getElementById('sendButton');
    if (sendButton) {
      sendButton.disabled = true;
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
  }

  /**
   * Handle incoming WebSocket message
   */
  handleMessage(event) {
    const data = JSON.parse(event.data);
    console.log('Received:', data.type);
    console.log('Data:', data);

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
    const status = document.getElementById('connectionStatus');
    if (!status) return;

    if (connected) {
      status.className = 'connection-status connected';
      status.innerHTML = '<span class="status-dot"></span><span>Connected</span>';
    } else {
      status.className = 'connection-status disconnected';
      status.innerHTML = '<span class="status-dot"></span><span>Disconnected</span>';
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
    }, CONFIG.RECONNECT_DELAY);
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
