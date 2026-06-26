/**
 * WebSocket connection management with auto-reconnection
 * Supports both native WebSocket and ActionCable connections
 */

import { getWebSocketUrl, getRailsUrl } from '../config.js';
import { ActionCableAdapter } from './ActionCableAdapter.js';
import { TokenManager } from '../auth/TokenManager.js';

// Control messages that must NOT be queued for replay after a reconnect:
//  - `auth` tokens are regenerated fresh on every (re)connect, so a stale one is useless.
//  - `cancel` only means something for the run that was live when it was issued;
//    replaying it after reconnect could cancel a brand-new run.
const NON_QUEUEABLE_TYPES = new Set(['auth', 'cancel']);

export class WebSocketManager {
  constructor(messageHandler, config = {}, elements = {}) {
    this.messageHandler = messageHandler;
    this.config = config;
    this.elements = elements;
    this.socket = null;
    this.reconnectTimer = null;
    this.isActionCable = false;
    this.isAuthenticated = false;
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = config.maxReconnectAttempts ?? 5;
    // Messages sent while the socket is down wait here and are delivered once we
    // reconnect & re-authenticate. Bounded so a runaway producer can't grow it
    // without limit.
    this.outbox = [];
    this.maxQueueSize = config.maxQueueSize ?? 25;
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
    this.wsUrl = wsUrl;
    this.socket = new WebSocket(wsUrl);
    this.isActionCable = false;

    this.socket.onopen = () => this.handleOpen();
    this.socket.onclose = (event) => this.handleClose(event);
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
    this.socket.onclose = (event) => this.handleClose(event);
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
    this.reconnectAttempts = 0;

    if (this.elements.sendButton) {
      this.elements.sendButton.disabled = false;
    }

    // Emit custom event
    window.dispatchEvent(new CustomEvent('websocketConnected'));

    // Send authentication token (for native WebSocket connections only)
    // ActionCable connections handle auth via the Rails gem
    if (!this.isActionCable) {
      // Native sockets authenticate first; the queued outbox is flushed on
      // `auth_success` so user messages are never sent ahead of the handshake.
      this.sendAuthMessage();
    } else {
      // ActionCable is authenticated by the Rails gem, so it's ready immediately.
      this.flushOutbox();
    }
  }

  /**
   * Send authentication message with JWT token
   */
  async sendAuthMessage() {
    try {
      const token = await TokenManager.getToken();
      if (token) {
        this.send({ type: 'auth', token: token });
      } else {
        console.warn('No auth token available - WebSocket may be unauthenticated');
      }
    } catch (error) {
      console.error('Failed to send auth message:', error);
    }
  }

  /**
   * Handle WebSocket close event
   */
  handleClose(event) {
    const closeInfo = event ? {
      code: event.code,
      reason: event.reason,
      wasClean: event.wasClean,
      url: this.wsUrl,
      isActionCable: this.isActionCable,
      attempt: this.reconnectAttempts,
      maxAttempts: this.maxReconnectAttempts
    } : {
      url: this.wsUrl,
      isActionCable: this.isActionCable,
      attempt: this.reconnectAttempts,
      maxAttempts: this.maxReconnectAttempts
    };
    console.warn('WebSocket closed:', closeInfo);

    this.updateConnectionStatus(false);

    if (this.elements.sendButton) {
      this.elements.sendButton.disabled = true;
    }

    // Emit custom event with close info
    window.dispatchEvent(new CustomEvent('websocketDisconnected', { detail: closeInfo }));

    // Attempt to reconnect after delay
    this.scheduleReconnect();
  }

  /**
   * Handle WebSocket error event
   *
   * Note: the browser's WebSocket `error` event is intentionally opaque for
   * security reasons — it carries no diagnostic detail. The real signal lives
   * in the `close` event (`code` / `reason`) that fires immediately after.
   */
  handleError(error) {
    const readyState = this.socket ? this.socket.readyState : null;
    console.error('WebSocket error:', {
      url: this.wsUrl,
      isActionCable: this.isActionCable,
      readyState,
      event: error
    });

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

    // Handle authentication responses
    if (data.type === 'auth_success') {
      this.isAuthenticated = true;
      console.log('WebSocket authenticated as:', data.user);
      // Now that we're live & authenticated, deliver anything that was queued
      // while the connection was down.
      this.flushOutbox();
      return;
    }

    if (data.type === 'auth_error') {
      this.isAuthenticated = false;
      console.error('WebSocket auth failed:', data.content);
      // Clear cached token so we fetch a fresh one on reconnect
      TokenManager.clearToken();
      return;
    }

    if (data.type === 'auth_warning') {
      console.warn('WebSocket auth warning:', data.content);
      // Try to authenticate again
      this.sendAuthMessage();
      return;
    }

    // Delegate to message handler
    if (this.messageHandler) {
      this.messageHandler.handleMessage(data);
    }
  }

  /**
   * Send a message over the socket.
   *
   * If the socket is open, the message goes out immediately. If it's down, a
   * user-meaningful message is queued and delivered automatically once we
   * reconnect (and a reconnect is nudged into motion), so a transient drop
   * never silently loses what the user sent. Ephemeral control messages
   * (auth/cancel) are dropped instead — replaying them after reconnect is wrong.
   */
  send(data) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(data));
      return true;
    }

    if (data && NON_QUEUEABLE_TYPES.has(data.type)) {
      console.warn(`WebSocket not connected; dropping "${data.type}" message`);
      return false;
    }

    this.queueMessage(data);
    this.ensureConnecting();
    return true; // accepted — will be delivered on reconnect
  }

  /**
   * Buffer an outbound message to deliver after we reconnect. Deduped by
   * reference so the same payload can't be queued twice, and bounded so the
   * oldest message is dropped if the queue overflows.
   */
  queueMessage(data) {
    if (this.outbox.includes(data)) return;

    this.outbox.push(data);
    if (this.outbox.length > this.maxQueueSize) {
      this.outbox.shift();
      console.warn('Outbound WebSocket queue full; dropped oldest message');
    } else {
      console.warn(`WebSocket not connected; queued message (${this.outbox.length} pending)`);
    }
  }

  /**
   * True if this exact payload (by reference) is already waiting in the outbox.
   * Lets the higher-level resume logic avoid double-sending a message the
   * outbox already owns.
   */
  hasQueued(data) {
    return this.outbox.includes(data);
  }

  /** Drop every queued message — used when we've given up reconnecting. */
  clearQueue() {
    this.outbox = [];
  }

  /**
   * Nudge a reconnect when a send lands on a dead socket. An active user action
   * is a strong signal to keep trying, so we refresh the retry budget even if
   * earlier automatic attempts had been exhausted. ActionCable self-heals via
   * the Rails gem, so we leave its reconnection alone.
   */
  ensureConnecting() {
    if (this.isActionCable) return;

    const state = this.socket ? this.socket.readyState : WebSocket.CLOSED;
    if (state === WebSocket.OPEN || state === WebSocket.CONNECTING || this.reconnectTimer) {
      return;
    }

    this.reconnectAttempts = 0; // user is actively trying — give a full budget
    this.scheduleReconnect();
  }

  /**
   * Deliver any queued messages once the connection is live and authenticated.
   * Re-uses send(), so if the socket dies again mid-flush the remaining
   * messages simply re-queue for the next reconnect.
   */
  flushOutbox() {
    if (!this.outbox.length) return;

    const pending = this.outbox;
    this.outbox = [];
    console.log(`Flushing ${pending.length} queued WebSocket message(s) after reconnect`);
    pending.forEach((data) => this.send(data));
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
   *
   * Caps retries at `maxReconnectAttempts`. When exhausted, dispatches
   * `websocketReconnectFailed` so the UI can show the "Lost connection"
   * error only after we've truly given up — not on transient drops.
   */
  scheduleReconnect() {
    // ActionCable handles reconnection automatically, skip for ActionCable
    if (this.isActionCable) {
      return;
    }

    if (this.reconnectAttempts >= this.maxReconnectAttempts) {
      console.error(`WebSocket reconnect failed after ${this.reconnectAttempts} attempts`);
      window.dispatchEvent(new CustomEvent('websocketReconnectFailed', {
        detail: {
          attempts: this.reconnectAttempts,
          maxAttempts: this.maxReconnectAttempts,
          url: this.wsUrl
        }
      }));
      return;
    }

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
    }

    this.reconnectAttempts += 1;
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
    this.clearQueue();

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
