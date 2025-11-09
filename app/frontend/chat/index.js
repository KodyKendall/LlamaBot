/**
 * Main entry point for the chat application
 * Initializes and coordinates all modules
 */

import { DEFAULT_CONFIG, getRailsUrl } from './config.js';
import { setCookie, getCookie } from './utils/cookies.js';
import { AppState } from './state/AppState.js';
import { StreamingState } from './state/StreamingState.js';
import { MessageRenderer } from './messages/MessageRenderer.js';
import { WebSocketManager } from './websocket/WebSocketManager.js';
import { MessageHandler } from './websocket/MessageHandler.js';
import { ScrollManager } from './ui/ScrollManager.js';
import { IframeManager } from './ui/IframeManager.js';
import { ElementSelector } from './ui/ElementSelector.js';
import { MenuManager } from './ui/MenuManager.js';
import { MobileViewManager } from './ui/MobileViewManager.js';
import { ThreadManager } from './threads/ThreadManager.js';
import { LoadingVerbs } from './utils/LoadingVerbs.js';

/**
 * Main application class - LlamaBot Client
 */
class ChatApp {
  constructor(containerSelector = 'body', userConfig = {}) {
    // Store container reference
    this.container = document.querySelector(containerSelector);
    if (!this.container) {
      throw new Error(`Container not found: ${containerSelector}`);
    }

    // Generate unique instance ID
    this.instanceId = `llamabot-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;

    // Merge configuration (deep merge for nested objects)
    this.config = this.mergeConfig(DEFAULT_CONFIG, userConfig);

    // Initialize state
    this.appState = new AppState();
    this.streamingState = new StreamingState();

    // Initialize UI components
    this.messageRenderer = null;
    this.scrollManager = null;
    this.iframeManager = null;
    this.elementSelector = null;
    this.menuManager = null;
    this.mobileViewManager = null;
    this.threadManager = null;
    this.loadingVerbs = new LoadingVerbs();

    // Initialize WebSocket components
    this.webSocketManager = null;
    this.messageHandler = null;

    // Store element references (will be populated in initComponents)
    this.elements = {};
  }

  /**
   * Deep merge two configuration objects
   */
  mergeConfig(defaults, userConfig) {
    const result = { ...defaults };

    for (const key in userConfig) {
      if (userConfig[key] !== null && typeof userConfig[key] === 'object' && !Array.isArray(userConfig[key])) {
        result[key] = this.mergeConfig(defaults[key] || {}, userConfig[key]);
      } else {
        result[key] = userConfig[key];
      }
    }

    return result;
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
    // Cache DOM element references (scoped to container)
    this.cacheElements();

    // Initialize UI managers (pass container and config)
    this.scrollManager = new ScrollManager(this.elements.messageHistory);
    this.iframeManager = new IframeManager(this.container);
    this.menuManager = new MenuManager(this.container);
    this.mobileViewManager = new MobileViewManager(this.scrollManager, this.container, this.elements);

    // Initialize message renderer with iframe manager, debug info callback, scroll manager, and loading verbs
    this.messageRenderer = new MessageRenderer(
      this.elements.messageHistory,
      this.iframeManager,
      (callback) => this.getRailsDebugInfo(callback),
      this.scrollManager,
      this.loadingVerbs,
      this.config,
      this.container,
      this.elements
    );

    // Initialize thread manager
    this.threadManager = new ThreadManager(
      this.messageRenderer,
      this.menuManager,
      this.scrollManager
    );

    // Initialize message handler
    this.messageHandler = new MessageHandler(
      this.appState,
      this.streamingState,
      this.messageRenderer,
      this.iframeManager,
      this.scrollManager,
      this.config
    );

    // Initialize WebSocket (pass config and elements)
    this.webSocketManager = new WebSocketManager(this.messageHandler, this.config, this.elements);
    const socket = this.webSocketManager.connect();
    this.appState.setSocket(socket);

    // Initialize event listeners
    this.initEventListeners();

    // Initialize iframe controls
    this.iframeManager.initNavigationButtons();
    this.iframeManager.initTabSwitching();
    this.iframeManager.initViewModeToggle();
    this.iframeManager.initUrlNavigation();

    // Initialize element selector
    this.elementSelector = new ElementSelector(this.iframeManager);
    this.elementSelector.init(this.elements.elementSelectorBtn, this.elements.messageInput);

    // Load threads
    this.threadManager.fetchThreads();

    // Load settings from cookies
    this.loadSettingsFromCookies();

    console.log(`LlamaBot instance ${this.instanceId} initialized`);
  }

  /**
   * Cache DOM element references scoped to this instance's container
   */
  cacheElements() {
    this.elements = {
      messageHistory: this.container.querySelector('[data-llamabot="message-history"]'),
      messageInput: this.container.querySelector('[data-llamabot="message-input"]'),
      sendButton: this.container.querySelector('[data-llamabot="send-button"]'),
      agentModeSelect: this.container.querySelector('[data-llamabot="agent-mode-select"]'),
      modelSelect: this.container.querySelector('[data-llamabot="model-select"]'),
      thinkingArea: this.container.querySelector('[data-llamabot="thinking-area"]'),
      elementSelectorBtn: this.container.querySelector('[data-llamabot="element-selector-btn"]'),
      connectionStatus: this.container.querySelector('[data-llamabot="connection-status"]'),
      hamburgerMenu: this.container.querySelector('[data-llamabot="hamburger-menu"]'),
      menuDrawer: this.container.querySelector('[data-llamabot="menu-drawer"]'),
      scrollToBottomBtn: this.container.querySelector('[data-llamabot="scroll-to-bottom"]'),
      liveSiteFrame: this.container.querySelector('[data-llamabot="live-site-frame"]'),
      vsCodeFrame: this.container.querySelector('[data-llamabot="vscode-frame"]')
    };
  }

  /**
   * Initialize event listeners
   */
  initEventListeners() {
    // Send button
    if (this.elements.sendButton) {
      this.elements.sendButton.addEventListener('click', () => this.sendMessageWithDebugInfo());
    }

    // Message input
    if (this.elements.messageInput) {
      this.elements.messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          e.preventDefault();
          this.sendMessageWithDebugInfo();
        }
      });
    }

    // Agent mode selector
    if (this.elements.agentModeSelect) {
      this.elements.agentModeSelect.addEventListener('change', (e) => {
        this.appState.setAgentMode(e.target.value);
        setCookie('agentMode', e.target.value, this.config.cookieExpiryDays);
        this.updateDropdownLabel(this.elements.agentModeSelect);
      });
      // Initialize with short label
      this.updateDropdownLabel(this.elements.agentModeSelect);
    }

    // Model selector
    if (this.elements.modelSelect) {
      this.elements.modelSelect.addEventListener('change', (e) => {
        setCookie('llmModel', e.target.value, this.config.cookieExpiryDays);
        this.updateDropdownLabel(this.elements.modelSelect);
      });
      // Initialize with short label
      this.updateDropdownLabel(this.elements.modelSelect);
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
    const input = this.elements.messageInput;
    if (!input) return;

    let message = input.value.trim();
    const agentMode = this.elements.agentModeSelect?.value;
    const llmModel = this.elements.modelSelect?.value || 'claude-4.5-haiku';

    if (!message || !this.webSocketManager) return;

    // Check if there's a selected element and append it to the message
    const selectedHTML = this.elementSelector?.getSelectedElementHTML();
    if (selectedHTML) {
      message = `${message}\n\n<SELECTED_ELEMENT>\n${selectedHTML}\n</SELECTED_ELEMENT>`;
    }

    // Reset state
    this.appState.resetMessageState();
    this.streamingState.reset();
    this.iframeManager.removeStreamingOverlay();

    // Add user message (show original without HTML)
    this.messageRenderer.addMessage(input.value.trim(), 'human', null);

    // Show thinking indicator in the dedicated thinking area
    if (this.elements.thinkingArea) {
      const verb = this.loadingVerbs.getRandomVerb();
      this.elements.thinkingArea.innerHTML = `<div class="typing-indicator">🦙 ${verb}...</div>`;
      this.elements.thinkingArea.classList.remove('hidden');

      // Start cycling the verb in the thinking area
      const thinkingDiv = this.elements.thinkingArea.querySelector('.typing-indicator');
      if (thinkingDiv) {
        this.loadingVerbs.startCycling(thinkingDiv);
      }
    }

    // Change placeholder text while thinking
    input.placeholder = 'Queue another message...';

    // Don't create content message yet - it will be created on first content chunk
    // This prevents empty message boxes from showing up
    this.appState.setCurrentAiMessage(null);

    // Clear input
    input.value = '';
    input.style.height = 'auto';

    // Clear selected element badge
    if (this.elementSelector) {
      this.elementSelector.clearSelection();
    }

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

    // Call custom callback if provided
    if (this.config.onMessageReceived) {
      this.config.onMessageReceived({ message, threadId, agentMode, llmModel });
    }
  }

  /**
   * Get Rails debug info via postMessage
   */
  getRailsDebugInfo(callback, timeout = null) {
    const iframe = this.elements.liveSiteFrame;
    const timeoutMs = timeout || this.config.railsDebugTimeout;

    if (!iframe || !iframe.contentWindow) {
      callback(new Error("Iframe not available"));
      return;
    }

    const messageId = Math.random().toString(36).substring(2, 11);

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
    }, timeoutMs);
  }

  /**
   * Load settings from cookies
   */
  loadSettingsFromCookies() {
    const savedMode = getCookie('agentMode');
    if (savedMode && this.elements.agentModeSelect) {
      if (Array.from(this.elements.agentModeSelect.options).some(option => option.value === savedMode)) {
        this.elements.agentModeSelect.value = savedMode;
        this.appState.setAgentMode(savedMode);
        this.updateDropdownLabel(this.elements.agentModeSelect);
      }
    }

    const savedModel = getCookie('llmModel');
    if (savedModel && this.elements.modelSelect) {
      if (Array.from(this.elements.modelSelect.options).some(option => option.value === savedModel)) {
        this.elements.modelSelect.value = savedModel;
        this.updateDropdownLabel(this.elements.modelSelect);
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

/**
 * LlamaBot Client Library - Public API
 * Single global entry point for creating chat instances
 */
window.LlamaBot = {
  version: '0.1.0',

  /**
   * Create a new LlamaBot chat instance
   * @param {string} containerSelector - CSS selector for the container element
   * @param {Object} config - Configuration options to override defaults
   * @returns {ChatApp} The chat application instance
   */
  create: (containerSelector, config = {}) => {
    const instance = new ChatApp(containerSelector, config);
    instance.init();
    return instance;
  },

  /**
   * Default configuration (read-only reference)
   */
  get defaultConfig() {
    return { ...DEFAULT_CONFIG };
  }
};

// Export ChatApp class for advanced usage
export { ChatApp, DEFAULT_CONFIG };

// Auto-initialize for backward compatibility (if body contains chat elements)
// This maintains the existing single-page app behavior
if (document.querySelector('[data-llamabot="message-history"]')) {
  const legacyInstance = window.LlamaBot.create('body');
  // Expose for debugging (legacy behavior)
  window.chatApp = legacyInstance;
}
