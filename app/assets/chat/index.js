/**
 * Main entry point for the chat application
 * Initializes and coordinates all modules
 */

import { CONFIG, getRailsUrl } from './config.js';
import { setCookie, getCookie } from './utils/cookies.js';
import { AppState } from './state/AppState.js';
import { StreamingState } from './state/StreamingState.js';
import { MessageRenderer } from './messages/MessageRenderer.js';
import { WebSocketManager } from './websocket/WebSocketManager.js';
import { MessageHandler } from './websocket/MessageHandler.js';
import { ScrollManager } from './ui/ScrollManager.js';
import { IframeManager } from './ui/IframeManager.js';
import { MenuManager } from './ui/MenuManager.js';
import { MobileViewManager } from './ui/MobileViewManager.js';
import { ThreadManager } from './threads/ThreadManager.js';
import { LoadingVerbs } from './utils/LoadingVerbs.js';

/**
 * Main application class
 */
class ChatApp {
  constructor() {
    // Initialize state
    this.appState = new AppState();
    this.streamingState = new StreamingState();

    // Initialize UI components
    this.messageRenderer = null;
    this.scrollManager = null;
    this.iframeManager = null;
    this.menuManager = null;
    this.mobileViewManager = null;
    this.threadManager = null;
    this.loadingVerbs = new LoadingVerbs();

    // Initialize WebSocket components
    this.webSocketManager = null;
    this.messageHandler = null;
  }

  /**
   * Initialize the application
   */
  init() {
    // Wait for DOM to be ready
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', () => this.initComponents());
    } else {
      this.initComponents();
    }
  }

  /**
   * Initialize all components
   */
  initComponents() {
    // Initialize UI managers
    const messageHistoryElement = document.getElementById('message-history');
    this.scrollManager = new ScrollManager(messageHistoryElement);
    this.iframeManager = new IframeManager();
    this.menuManager = new MenuManager();
    this.mobileViewManager = new MobileViewManager(this.scrollManager);

    // Initialize message renderer with iframe manager, debug info callback, scroll manager, and loading verbs
    this.messageRenderer = new MessageRenderer(
      messageHistoryElement,
      this.iframeManager,
      (callback) => this.getRailsDebugInfo(callback),
      this.scrollManager,
      this.loadingVerbs
    );

    // Initialize thread manager
    this.threadManager = new ThreadManager(
      this.messageRenderer,
      this.menuManager,
      this.scrollManager
    );

    // Make threadManager globally accessible for onclick handlers
    window.threadManager = this.threadManager;
    window.mobileViewManager = this.mobileViewManager;

    // Initialize message handler
    this.messageHandler = new MessageHandler(
      this.appState,
      this.streamingState,
      this.messageRenderer,
      this.iframeManager,
      this.scrollManager
    );

    // Initialize WebSocket
    this.webSocketManager = new WebSocketManager(this.messageHandler);
    const socket = this.webSocketManager.connect();
    this.appState.setSocket(socket);

    // Initialize event listeners
    this.initEventListeners();

    // Initialize iframe controls
    this.iframeManager.initNavigationButtons();
    this.iframeManager.initTabSwitching();
    this.iframeManager.initViewModeToggle();
    this.iframeManager.initUrlNavigation();

    // Load threads
    this.threadManager.fetchThreads();

    // Load settings from cookies
    this.loadSettingsFromCookies();

    console.log('Chat application initialized');
  }

  /**
   * Initialize event listeners
   */
  initEventListeners() {
    // Send button
    const sendButton = document.getElementById('sendButton');
    if (sendButton) {
      sendButton.addEventListener('click', () => this.sendMessageWithDebugInfo());
    }

    // Message input
    const messageInput = document.getElementById('messageInput');
    if (messageInput) {
      messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          this.sendMessageWithDebugInfo();
        }
      });
    }

    // Agent mode selector
    const agentModeSelect = document.getElementById('agentModeSelect');
    if (agentModeSelect) {
      agentModeSelect.addEventListener('change', (e) => {
        this.appState.setAgentMode(e.target.value);
        setCookie('agentMode', e.target.value, CONFIG.COOKIE_EXPIRY_DAYS);
        this.updateDropdownLabel(agentModeSelect);
      });
      // Initialize with short label
      this.updateDropdownLabel(agentModeSelect);
    }

    // Model selector
    const modelSelect = document.getElementById('modelSelect');
    if (modelSelect) {
      modelSelect.addEventListener('change', (e) => {
        setCookie('llmModel', e.target.value, CONFIG.COOKIE_EXPIRY_DAYS);
        this.updateDropdownLabel(modelSelect);
      });
      // Initialize with short label
      this.updateDropdownLabel(modelSelect);
    }

    // LEGACY: Listen for stream end event
    // This was used for HTML streaming preview feature
    // Keeping for backwards compatibility but may be deprecated
    window.addEventListener('streamEnded', () => {
      // Refresh Rails app after agent finishes making changes
      this.iframeManager.refreshRailsApp((callback) => this.getRailsDebugInfo(callback));
    });

    // Listen for iframe refresh requested (triggered by refresh button)
    window.addEventListener('iframeRefreshRequested', () => {
      // Refresh Rails app to show latest changes
      this.iframeManager.refreshRailsApp((callback) => this.getRailsDebugInfo(callback));
    });

    // Listen for thread change
    window.addEventListener('threadChanged', (e) => {
      this.appState.setThreadId(e.detail.threadId);
    });

    // Listen for new thread creation
    window.addEventListener('createNewThread', () => {
      this.threadManager.createNewThread();
    });
  }

  /**
   * Send message with debug info
   */
  sendMessageWithDebugInfo() {
    this.getRailsDebugInfo((debugInfoJson) => {
      this.sendMessage(debugInfoJson);
    });
  }

  /**
   * Send message via WebSocket
   */
  sendMessage(debugInfo = null) {
    const input = document.getElementById('messageInput');
    const message = input.value.trim();
    const agentMode = document.getElementById('agentModeSelect')?.value;
    const llmModel = document.getElementById('modelSelect')?.value;

    if (!message || !this.webSocketManager) return;

    // Reset state
    this.appState.resetMessageState();
    this.streamingState.reset();
    this.iframeManager.removeStreamingOverlay();

    // Add user message
    this.messageRenderer.addMessage(message, 'human', null);

    // Show thinking indicator in the dedicated thinking area
    const thinkingArea = document.getElementById('thinkingArea');
    if (thinkingArea) {
      const verb = this.loadingVerbs.getRandomVerb();
      thinkingArea.innerHTML = `<div class="typing-indicator">🦙 ${verb}...</div>`;
      thinkingArea.classList.remove('hidden');

      // Start cycling the verb in the thinking area
      const thinkingDiv = thinkingArea.querySelector('.typing-indicator');
      if (thinkingDiv) {
        this.loadingVerbs.startCycling(thinkingDiv);
      }
    }

    // Change placeholder text while thinking
    if (input) {
      input.placeholder = 'Queue another message...';
    }

    // Don't create content message yet - it will be created on first content chunk
    // This prevents empty message boxes from showing up
    this.appState.setCurrentAiMessage(null);

    // Clear input
    input.value = '';
    input.style.height = 'auto';

    // Ensure thread ID exists
    const threadId = this.appState.ensureThreadId();

    // Force scroll to bottom for user messages
    this.scrollManager.scrollToBottom(true);

    // Send message
    const messageData = {
      message: message,
      thread_id: threadId,
      origin: window.location.host,
      debug_info: debugInfo,
      agent_name: this.appState.getAgentConfig().name,
      agent_mode: agentMode,
      llm_model: llmModel
    };

    this.webSocketManager.send(messageData);
  }

  /**
   * Get Rails debug info via postMessage
   */
  getRailsDebugInfo(callback, timeout = CONFIG.RAILS_DEBUG_TIMEOUT) {
    const iframe = document.getElementById('liveSiteFrame');

    if (!iframe || !iframe.contentWindow) {
      callback(new Error("Iframe not available"));
      return;
    }

    const messageId = Math.random().toString(36).substr(2, 9);

    function handleMessage(event) {
      if (event.data && event.data.source === "llamapress") {
        window.removeEventListener("message", handleMessage);
        clearTimeout(timer);
        callback(event.data);
      }
    }

    window.addEventListener("message", handleMessage);

    iframe.contentWindow.postMessage({
      source: 'leonardo',
      type: "get_debug_info",
      id: messageId
    }, "*");

    const timer = setTimeout(() => {
      window.removeEventListener("message", handleMessage);
      callback(new Error("No response from Rails iframe"));
    }, timeout);
  }

  /**
   * Load settings from cookies
   */
  loadSettingsFromCookies() {
    const savedMode = getCookie('agentMode');
    if (savedMode) {
      const selectElement = document.getElementById('agentModeSelect');
      if (selectElement) {
        if (Array.from(selectElement.options).some(option => option.value === savedMode)) {
          selectElement.value = savedMode;
          this.appState.setAgentMode(savedMode);
          this.updateDropdownLabel(selectElement);
        }
      }
    }

    const savedModel = getCookie('llmModel');
    if (savedModel) {
      const modelSelect = document.getElementById('modelSelect');
      if (modelSelect) {
        if (Array.from(modelSelect.options).some(option => option.value === savedModel)) {
          modelSelect.value = savedModel;
          this.updateDropdownLabel(modelSelect);
        }
      }
    }
  }

  /**
   * Update dropdown to show short label when closed
   */
  updateDropdownLabel(selectElement) {
    if (!selectElement) return;

    const selectedOption = selectElement.options[selectElement.selectedIndex];
    const shortLabel = selectedOption?.getAttribute('data-short-label');

    if (shortLabel) {
      // Store original text if not already stored
      if (!selectedOption.hasAttribute('data-original-label')) {
        selectedOption.setAttribute('data-original-label', selectedOption.textContent);
      }

      // Update to short label when not open
      selectedOption.textContent = shortLabel;

      // Restore full labels when dropdown opens
      selectElement.addEventListener('focus', function restoreLabels() {
        Array.from(selectElement.options).forEach(option => {
          const originalLabel = option.getAttribute('data-original-label');
          if (originalLabel) {
            option.textContent = originalLabel;
          }
        });
      }, { once: false });

      // Restore short label when dropdown closes
      selectElement.addEventListener('blur', () => {
        setTimeout(() => {
          this.updateDropdownLabel(selectElement);
        }, 150);
      }, { once: true });
    }
  }
}

// Create and initialize the app
const app = new ChatApp();
app.init();

// Export for debugging
window.chatApp = app;
