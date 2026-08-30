/**
 * WebSocket connection management with auto-reconnection
 * Supports both native WebSocket and ActionCable connections
 */

import { getWebSocketUrl } from '../config.js';
import { ActionCableAdapter } from './ActionCableAdapter.js';
import { TokenManager } from '../auth/TokenManager.js';

import { leoDiagnostics } from '../utils/LeoDiagnostics.js';
import { shouldResumeAfterReconnect, fetchAuthoritativeMessage, nextResumeGeneration, isStaleResume } from './StreamResume.js';
// Control messages that must NOT be queued for replay after a reconnect:
//  - `auth` tokens are regenerated fresh on every (re)connect, so a stale one is useless.
//  - `cancel` only means something for the run that was live when it was issued;
//    replaying it after reconnect could cancel a brand-new run.
//  - `attach` carries a point-in-time last_seq; it's only ever sent on a live
//    (re)connect, and replaying a stale one would mis-replay the run.
const NON_QUEUEABLE_TYPES = new Set(['auth', 'cancel', 'attach']);

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

    // NOTE: this used to also set `liveSiteFrame.src = getRailsUrl()` on https — a
    // leftover from before IframeManager existed. IframeManager.initIframeSources()
    // owns that iframe now and runs FIRST in the same initComponents() pass, so the
    // line was silently overwriting whatever the manager had just decided: the
    // restored last page, and the Unified Login consume URL. The websocket layer has
    // no business touching the preview iframe — don't put it back.

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
    leoDiagnostics.noteOpen(this.socket ? this.socket.readyState : null);
    this.reconnectAttempts = 0;
    this.clearActionCableWatchdog();

    // Reconnecting the pipe does not recover the turn. If a run was in flight when the
    // socket died, chunks sent during the dead window are gone for good, and appending
    // whatever arrives next produces an answer that starts mid-sentence. Re-sync instead.
    if (this.runWasInFlightAtClose) {
      this.runWasInFlightAtClose = false;
      this.resumeInFlightRun();
    }

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
      this.announceReady();
    }
  }

  /**
   * Announce that this socket may now carry authenticated traffic.
   *
   * `websocketConnected` fires as soon as the transport is up, which is too
   * early: the server refuses control frames (`attach`, `cancel`) until the
   * handshake lands, and the token fetch behind `sendAuthMessage` is async.
   * Anything that resumes a run waits for this event instead.
   */
  announceReady() {
    window.dispatchEvent(new CustomEvent('websocketReady'));
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
        // A box with WS_AUTH_REQUIRED=false has no token and never will; don't
        // strand the resume path waiting for a handshake that isn't coming.
        this.announceReady();
      }
    } catch (error) {
      console.error('Failed to send auth message:', error);
      this.announceReady();
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
    leoDiagnostics.noteClose({
      code: closeInfo.code,
      reason: closeInfo.reason,
      wasClean: closeInfo.wasClean,
      readyState: this.socket ? this.socket.readyState : null,
    });

    this.updateConnectionStatus(false);

    // Remember whether Leo was mid-answer when the pipe died. The thinking indicator is
    // the same signal the box already reports as "Lost connection mid-run", and it is what
    // decides on reconnect whether we must re-sync the reply or leave the thread alone.
    this.runWasInFlightAtClose = this.isRunInFlight();

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

    leoDiagnostics.noteError(readyState);

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
      this.announceReady();
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

    // Server acknowledged receipt of a user message. Surface the
    // client_message_id so the resume-on-reconnect logic knows the server
    // already has it and must not re-send it (which would duplicate the turn).
    if (data.type === 'ack') {
      window.dispatchEvent(new CustomEvent('websocketMessageAcked', {
        detail: { client_message_id: data.client_message_id, status: data.status }
      }));
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
    leoDiagnostics.noteOutbox(this.outbox.length, 'queue');
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
    leoDiagnostics.noteOutbox(0, 'flush');
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
    // ActionCable reconnects the transport itself, so we must NOT also drive connect().
    // But the old bare `return` here left two things broken on every ActionCable box:
    // handleClose() had already disabled the send button and nothing on this path ever
    // re-enabled it, and reconnectAttempts never moved — so `reconnect_attempts=0` in the
    // diagnostics was a dead gauge that told the last investigation the client had not
    // even tried. Count the attempt, and arm a watchdog so a reconnect that never lands
    // surfaces as a real failure instead of a permanently disabled composer.
    if (this.isActionCable) {
      this.reconnectAttempts += 1;
      leoDiagnostics.noteReconnect(this.reconnectAttempts, this.maxReconnectAttempts);
      this.armActionCableWatchdog();
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
    leoDiagnostics.noteReconnect(this.reconnectAttempts, this.maxReconnectAttempts);
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.config.reconnectDelay || 3000);
  }

  /**
   * Is Leo mid-answer right now?
   *
   * The visible thinking indicator is the signal — it is what the frontend already
   * reports as "Lost connection mid-run (thinking indicator active)", so resume keys off
   * the same fact the telemetry does rather than inventing a second notion of "running".
   */
  isRunInFlight() {
    const area = window.chatApp?.elements?.thinkingArea;
    return Boolean(area && !area.classList.contains('hidden'));
  }

  /**
   * Give ActionCable a bounded window to recover on its own.
   *
   * The old comment claimed ActionCable "handles reconnection automatically", but the
   * customer evidence is a UI stuck on "Lost connection" with input disabled until a
   * manual page reload (lohman, July 2026, twice). If the socket really does come back,
   * handleOpen() clears this and nothing is shown. If it does not, the user gets a real
   * failure they can act on instead of a dead composer.
   */
  armActionCableWatchdog() {
    this.clearActionCableWatchdog();
    this.actionCableWatchdog = setTimeout(() => {
      if (this.elements.sendButton) {
        this.elements.sendButton.disabled = false;
      }
      window.dispatchEvent(new CustomEvent('websocketReconnectFailed', {
        detail: {
          attempts: this.reconnectAttempts,
          maxAttempts: this.maxReconnectAttempts,
          url: this.wsUrl,
          transport: 'actioncable',
        }
      }));
    }, this.config.actionCableRecoveryMs || 15000);
  }

  clearActionCableWatchdog() {
    if (this.actionCableWatchdog) {
      clearTimeout(this.actionCableWatchdog);
      this.actionCableWatchdog = null;
    }
  }

  /**
   * Replace the orphaned partial bubble with what the server actually produced.
   *
   * Idempotent by generation token: the fetch is async and chunks may still be arriving,
   * so a slow answer from an older reconnect must never overwrite a newer render.
   * Fail-open throughout — if anything goes wrong we leave the existing bubble alone,
   * because a resume that breaks is worse than the truncation it is fixing.
   */
  async resumeInFlightRun() {
    const app = window.chatApp;
    const threadId = app?.appState?.getThreadId?.() || null;
    const partialText = app?.streamingState?.getCleanedFullMessage?.() || '';

    if (!shouldResumeAfterReconnect({ wasRunInFlight: true, threadId })) return;

    const generation = nextResumeGeneration(this.resumeGeneration);
    this.resumeGeneration = generation;

    const authoritative = await fetchAuthoritativeMessage(threadId, { partialText });

    // A newer resume started while we were waiting — discard this one.
    if (isStaleResume(generation, this.resumeGeneration)) return;
    if (!authoritative) return;

    // The run is over as far as this browser is concerned; a spinner left running is what
    // made the customer believe Leo had died.
    app?.hideThinkingIndicator?.();
    app?.replaceStreamingMessage?.(authoritative);
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
