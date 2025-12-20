/**
 * ActionCable adapter that provides a WebSocket-like interface
 * This allows LlamaBot's JavaScript to work with ActionCable connections
 */

export class ActionCableAdapter {
  constructor(consumer, channelConfig, messageHandler) {
    this.consumer = consumer;
    this.channelConfig = channelConfig;
    this.messageHandler = messageHandler;
    this.subscription = null;
    this.isConnected = false;

    // Callback handlers (set by WebSocketManager)
    this.onopen = null;
    this.onclose = null;
    this.onerror = null;
    this.onmessage = null;
  }

  /**
   * Connect to ActionCable channel
   * Mimics WebSocket connect() behavior
   */
  connect() {
    this.subscription = this.consumer.subscriptions.create(
      this.channelConfig,
      {
        connected: () => this.handleConnected(),
        disconnected: () => this.handleDisconnected(),
        received: (data) => this.handleReceived(data)
      }
    );

    return this;
  }

  /**
   * Handle ActionCable connected event
   */
  handleConnected() {
    this.isConnected = true;

    if (this.onopen) {
      this.onopen();
    }
  }

  /**
   * Handle ActionCable disconnected event
   */
  handleDisconnected() {
    this.isConnected = false;

    if (this.onclose) {
      this.onclose();
    }
  }

  /**
   * Handle ActionCable received event
   * Unwraps the ActionCable message format and passes to WebSocket handler
   */
  handleReceived(data) {
    try {
      // ActionCable wraps messages in {message: "..."} format
      // Parse the outer JSON
      const parsedData = JSON.parse(data);

      // Extract the inner message
      const messageData = parsedData.message;

      // Create a WebSocket-like event object
      const event = {
        data: JSON.stringify(messageData),
        type: 'message'
      };

      // Call the WebSocket onmessage handler
      if (this.onmessage) {
        this.onmessage(event);
      }
    } catch (error) {
      console.error('Error parsing ActionCable message:', error);
      if (this.onerror) {
        this.onerror(error);
      }
    }
  }

  /**
   * Send message via ActionCable
   * Mimics WebSocket send() behavior
   */
  send(data) {
    if (!this.subscription) {
      console.error('ActionCable subscription not established');
      return false;
    }

    try {
      // Parse the data to send as JSON
      const messageData = JSON.parse(data);

      // Send via ActionCable subscription
      this.subscription.send(messageData);
      return true;
    } catch (error) {
      console.error('Error sending ActionCable message:', error);
      if (this.onerror) {
        this.onerror(error);
      }
      return false;
    }
  }

  /**
   * Disconnect from ActionCable
   * Mimics WebSocket close() behavior
   */
  close() {
    if (this.subscription) {
      this.subscription.unsubscribe();
      this.subscription = null;
    }
    this.isConnected = false;
  }

  /**
   * Get connection ready state (mimics WebSocket readyState)
   */
  get readyState() {
    if (this.isConnected) {
      return 1; // WebSocket.OPEN
    }
    return 0; // WebSocket.CONNECTING
  }

  /**
   * Static constant for OPEN state (like WebSocket.OPEN)
   */
  static get OPEN() {
    return 1;
  }
}
