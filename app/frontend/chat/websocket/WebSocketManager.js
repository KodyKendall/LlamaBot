/**
 * WebSocket connection management with auto-reconnection
 * Supports both native WebSocket and ActionCable connections
 */

import { getWebSocketUrl, getRailsUrl } from '../config.js';
import { ActionCableAdapter } from './ActionCableAdapter.js';

export class WebSocketManager {
  constructor(messageHandler, config = {}, elements = {}) {
    this.messageHandler = messageHandler;
    this.config = config;
    this.elements = elements;
    this.socket = null;
    this.reconnectTimer = null;
    this.isActionCable = false;
  }

  /**
   * Initialize WebSocket connection
   * Supports both native WebSocket and ActionCable
   */
  connect() {
    // Check if ActionCable configuration is provided
    if (this.config.actionCable) {
      return this.connectActionCable();
    } else {
      return this.connectWebSocket();
    }
  }

  /**
   * Initialize native WebSocket connection
   */
  connectWebSocket() {
    const wsUrl = this.config.websocketUrl || getWebSocketUrl();
    this.socket = new WebSocket(wsUrl);
    this.isActionCable = false;

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
   * Initialize ActionCable connection
   */
  connectActionCable() {
    const { consumer, ...channelConfig } = this.config.actionCable;

    // Create ActionCable adapter with WebSocket-like interface
    this.socket = new ActionCableAdapter(
      consumer,
      channelConfig,
      this.messageHandler
    );
    this.isActionCable = true;

    // Set handlers
    this.socket.onopen = () => this.handleOpen();
    this.socket.onclose = () => this.handleClose();
    this.socket.onerror = (error) => this.handleError(error);
    this.socket.onmessage = (event) => this.handleMessage(event);

    // Connect
    this.socket.connect();

    return this.socket;
  }

  /**
   * Handle WebSocket open event
   */
  handleOpen() {
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
      // Apply custom CSS class if configured, otherwise use default
      if (this.config.cssClasses?.connectionStatusConnected) {
        this.elements.connectionStatus.className = this.config.cssClasses.connectionStatusConnected;
      } else {
        this.elements.connectionStatus.className = 'connection-status connected';
        this.elements.connectionStatus.innerHTML = '<span class="status-dot"></span>';
      }
    } else {
      // Apply custom CSS class if configured, otherwise use default
      if (this.config.cssClasses?.connectionStatusDisconnected) {
        this.elements.connectionStatus.className = this.config.cssClasses.connectionStatusDisconnected;
      } else {
        this.elements.connectionStatus.className = 'connection-status disconnected';
        this.elements.connectionStatus.innerHTML = '<span class="status-dot"></span>';
      }
    }
  }

  /**
   * Schedule reconnection attempt
   * Note: ActionCable handles reconnection automatically
   */
  scheduleReconnect() {
    // ActionCable handles reconnection automatically, skip for ActionCable
    if (this.isActionCable) {
      return;
    }

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }

    this.reconnectTimer = setTimeout(() => {
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
