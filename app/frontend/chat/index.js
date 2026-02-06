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
import { TokenIndicator } from './ui/TokenIndicator.js';
import { PromptManager } from './ui/PromptManager.js';
import { FileAttachmentManager } from './ui/FileAttachmentManager.js';
import { ThreadManager } from './threads/ThreadManager.js';
import { LoadingVerbs } from './utils/LoadingVerbs.js';
import { ClipboardFormatter } from './utils/ClipboardFormatter.js';

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
    this.tokenIndicator = null;
    this.promptManager = null;
    this.fileAttachmentManager = null;
    this.loadingVerbs = new LoadingVerbs();

    // Initialize WebSocket components
    this.webSocketManager = null;
    this.messageHandler = null;

    // Store element references (will be populated in initComponents)
    this.elements = {};

    // Activity tracking for lease management
    this.lastActivitySync = 0;
    this.ACTIVITY_SYNC_INTERVAL = 60000; // Sync to backend every 60 seconds max
    this.leaseConfig = null;
    this.inactivityCheckInterval = null;
  }

  /**
   * Deep merge two configuration objects
   * Avoids infinite recursion on DOM/ActionCable objects
   */
  mergeConfig(defaults, userConfig) {
    const result = { ...defaults };

    for (const key in userConfig) {
      const value = userConfig[key];

      // Skip null/undefined
      if (value === null || value === undefined) {
        result[key] = value;
        continue;
      }

      // Don't deep merge: DOM elements, ActionCable objects, or arrays
      if (
        value instanceof Element ||
        value.constructor?.name === 'Consumer' || // ActionCable Consumer
        value.constructor?.name === 'Subscription' || // ActionCable Subscription
        Array.isArray(value)
      ) {
        result[key] = value;
      }
      // Deep merge plain objects only
      else if (typeof value === 'object' && value.constructor === Object) {
        result[key] = this.mergeConfig(defaults[key] || {}, value);
      }
      // Primitive values
      else {
        result[key] = value;
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
    this.tokenIndicator = new TokenIndicator();
    this.clipboardFormatter = new ClipboardFormatter(this.elements.messageHistory);
    this.clipboardFormatter.init();

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
      this.tokenIndicator,
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

    // Close toolbar when element selector is clicked
    if (this.elements.elementSelectorBtn) {
      this.elements.elementSelectorBtn.addEventListener('click', () => {
        this.closeToolsToolbar();
      });
    }

    // Initialize prompt manager
    this.promptManager = new PromptManager();
    const inputArea = this.container.querySelector('.input-area');
    this.promptManager.init(this.elements.promptLibraryBtn, this.elements.messageInput, inputArea);

    // Close toolbar when prompt library is clicked
    if (this.elements.promptLibraryBtn) {
      this.elements.promptLibraryBtn.addEventListener('click', () => {
        this.closeToolsToolbar();
      });
    }

    // Initialize file attachment manager
    this.fileAttachmentManager = new FileAttachmentManager();
    this.fileAttachmentManager.init(
      this.elements.fileAttachBtn,
      this.elements.fileInput,
      this.elements.attachmentsPreview
    );

    // Close toolbar when file attach is clicked
    if (this.elements.fileAttachBtn) {
      this.elements.fileAttachBtn.addEventListener('click', () => {
        this.closeToolsToolbar();
      });
    }

    // Load threads
    this.threadManager.fetchThreads();

    // Setup activity tracking for lease management
    this.setupActivityTracking();

    // Load settings from cookies
    this.loadSettingsFromCookies();

    // Fetch available models and disable unavailable ones
    this.fetchAvailableModels();

    // Dispatch ready event for external scripts to hook into
    window.dispatchEvent(new CustomEvent('llamabot:ready', { detail: { instance: this } }));
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
      modelToggleBtn: this.container.querySelector('[data-llamabot="model-toggle-btn"]'),
      modelSelectorContainer: this.container.querySelector('[data-llamabot="model-selector-container"]'),
      thinkingArea: this.container.querySelector('[data-llamabot="thinking-area"]'),
      elementSelectorBtn: this.container.querySelector('[data-llamabot="element-selector-btn"]'),
      captureLogsBtn: this.container.querySelector('[data-llamabot="capture-logs-btn"]'),
      connectionStatus: this.container.querySelector('[data-llamabot="connection-status"]'),
      hamburgerMenu: this.container.querySelector('[data-llamabot="hamburger-menu"]'),
      menuDrawer: this.container.querySelector('[data-llamabot="menu-drawer"]'),
      scrollToBottomBtn: this.container.querySelector('[data-llamabot="scroll-to-bottom"]'),
      liveSiteFrame: this.container.querySelector('[data-llamabot="live-site-frame"]'),
      vsCodeFrame: this.container.querySelector('[data-llamabot="vscode-frame"]'),
      promptLibraryBtn: this.container.querySelector('[data-llamabot="prompt-library-btn"]'),
      fileAttachBtn: this.container.querySelector('[data-llamabot="file-attach-btn"]'),
      fileInput: this.container.querySelector('[data-llamabot="file-input"]'),
      attachmentsPreview: this.container.querySelector('[data-llamabot="attachments-preview"]'),
      toolsToggleBtn: this.container.querySelector('[data-llamabot="tools-toggle-btn"]'),
      toolsToolbar: this.container.querySelector('[data-llamabot="tools-toolbar"]')
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

    // Tools toolbar toggle
    if (this.elements.toolsToggleBtn && this.elements.toolsToolbar) {
      this.elements.toolsToggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const isHidden = this.elements.toolsToolbar.classList.contains('hidden');
        if (isHidden) {
          this.elements.toolsToolbar.classList.remove('hidden');
          this.elements.toolsToggleBtn.classList.add('active');
        } else {
          this.elements.toolsToolbar.classList.add('hidden');
          this.elements.toolsToggleBtn.classList.remove('active');
        }
      });

      // Close toolbar when clicking outside
      document.addEventListener('click', (e) => {
        if (this.elements.toolsToolbar &&
            !this.elements.toolsToolbar.contains(e.target) &&
            !this.elements.toolsToggleBtn.contains(e.target)) {
          this.elements.toolsToolbar.classList.add('hidden');
          this.elements.toolsToggleBtn.classList.remove('active');
        }
      });
    }

    // Model toggle button - show/hide model selector (inside toolbar)
    if (this.elements.modelToggleBtn && this.elements.modelSelectorContainer) {
      this.elements.modelToggleBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const isHidden = this.elements.modelSelectorContainer.classList.contains('hidden');
        if (isHidden) {
          this.elements.modelSelectorContainer.classList.remove('hidden');
          this.elements.modelToggleBtn.classList.add('active');
          // Close toolbar when model selector opens
          this.closeToolsToolbar();
        } else {
          this.elements.modelSelectorContainer.classList.add('hidden');
          this.elements.modelToggleBtn.classList.remove('active');
        }
      });

      // Close model selector when clicking outside
      document.addEventListener('click', (e) => {
        if (!this.elements.modelToggleBtn.contains(e.target) &&
            !this.elements.modelSelectorContainer.contains(e.target)) {
          this.elements.modelSelectorContainer.classList.add('hidden');
          this.elements.modelToggleBtn.classList.remove('active');
        }
      });
    }

    // Suggested prompts - handle clicks on all buttons with data-llamabot="suggested-prompt"
    const suggestedPrompts = this.container.querySelectorAll('[data-llamabot="suggested-prompt"]');
    suggestedPrompts.forEach(button => {
      button.addEventListener('click', (e) => {
        const promptText = e.target.textContent || e.target.innerText;
        if (this.elements.messageInput) {
          this.elements.messageInput.value = promptText;
          // Optionally auto-send the message
          if (this.config.autoSendSuggestedPrompts !== false) {
            this.sendMessageWithDebugInfo();
          }
        }
      });
    });

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
      // Reset token indicator when switching threads (we don't have historical token counts)
      if (this.tokenIndicator) {
        this.tokenIndicator.reset();
      }
    });

    // Listen for new thread creation
    window.addEventListener('createNewThread', () => {
      this.threadManager.createNewThread();
      // Reset token indicator for new conversation
      if (this.tokenIndicator) {
        this.tokenIndicator.reset();
      }
    });

    // Listen for launchpad commands from Rails iframe (prefill chat input)
    window.addEventListener('message', (event) => {
      if (event.data && event.data.source === 'launchpad' && event.data.type === 'prefill-chat') {
        const command = event.data.command;
        if (command && this.elements.messageInput) {
          this.elements.messageInput.value = command;
          this.elements.messageInput.focus();
          // Trigger input event to update send button state
          this.elements.messageInput.dispatchEvent(new Event('input', { bubbles: true }));
          console.log('Launchpad: Prefilled chat with command:', command);
        }
      }
    });

    // Capture Rails logs button
    if (this.elements.captureLogsBtn) {
      this.elements.captureLogsBtn.addEventListener('click', async () => {
        // Close toolbar
        this.closeToolsToolbar();

        this.elements.captureLogsBtn.classList.add('recording');
        try {
          // Clear old JS logs first
          this.clearJsConsoleLogs();

          // Wait for Rails logs (10 seconds) - JS logs accumulate during this time
          const railsLogsRes = await fetch('/api/capture-rails-logs', { method: 'POST' }).then(r => r.json());

          // Now fetch JS logs that accumulated during the 10s recording period
          const jsLogs = await this.getJsConsoleLogs();

          // Format combined output
          let output = '';
          if (jsLogs && jsLogs.length > 0) {
            const formattedJsLogs = jsLogs.map(l => `[${l.type}] ${l.args.join(' ')}`).join('\n');
            output += `JavaScript Console Logs:\n\`\`\`\n${formattedJsLogs}\n\`\`\`\n\n`;
          }
          output += `Rails Server Logs:\n\`\`\`\n${railsLogsRes.logs}\n\`\`\``;

          if (this.elements.messageInput) {
            const existing = this.elements.messageInput.value;
            const separator = existing ? '\n\n' : '';
            this.elements.messageInput.value = existing + separator + output;
            this.elements.messageInput.dispatchEvent(new Event('input', { bubbles: true }));
          }
        } catch (err) {
          console.error('Failed to capture logs:', err);
        } finally {
          this.elements.captureLogsBtn.classList.remove('recording');
        }
      });
    }
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
    const llmModel = this.elements.modelSelect?.value || 'gemini-3-flash';

    if (!message || !this.webSocketManager) return;

    // Prepend prompt content if a prompt is selected
    const promptContent = this.promptManager?.getSelectedPromptContent();
    if (promptContent) {
      message = `${promptContent}\n\n${message}`;
    }

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

    // Clear selected prompt badge
    if (this.promptManager) {
      this.promptManager.clearSelection();
    }

    // Get file attachments before clearing
    const attachments = this.fileAttachmentManager?.getAttachments() || [];

    // Clear file attachments
    if (this.fileAttachmentManager) {
      this.fileAttachmentManager.clearAttachments();
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
      llm_model: llmModel,
      attachments: attachments
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
   * Clear JavaScript console logs in the Rails iframe via postMessage
   */
  clearJsConsoleLogs() {
    const iframe = this.elements.liveSiteFrame;
    if (iframe && iframe.contentWindow) {
      iframe.contentWindow.postMessage({ source: 'leonardo', type: 'clear-console-logs' }, '*');
    }
  }

  /**
   * Get JavaScript console logs from the Rails iframe via postMessage
   */
  getJsConsoleLogs() {
    return new Promise((resolve) => {
      const iframe = this.elements.liveSiteFrame;
      if (!iframe || !iframe.contentWindow) {
        resolve([]);
        return;
      }

      const handleMessage = (event) => {
        if (event.data && event.data.source === 'llamapress' && event.data.type === 'console-logs') {
          window.removeEventListener('message', handleMessage);
          clearTimeout(timer);
          resolve(event.data.logs || []);
        }
      };

      window.addEventListener('message', handleMessage);
      iframe.contentWindow.postMessage({ source: 'leonardo', type: 'get-console-logs' }, '*');

      const timer = setTimeout(() => {
        window.removeEventListener('message', handleMessage);
        resolve([]); // Return empty if no response
      }, 2000);
    });
  }

  /**
   * Load settings from cookies
   */
  loadSettingsFromCookies() {
    const savedMode = getCookie('agentMode');
    if (this.elements.agentModeSelect) {
      if (savedMode && Array.from(this.elements.agentModeSelect.options).some(option => option.value === savedMode)) {
        // Cookie exists and is valid - use it
        this.elements.agentModeSelect.value = savedMode;
        this.appState.setAgentMode(savedMode);
        this.updateDropdownLabel(this.elements.agentModeSelect);
      } else {
        // No cookie OR invalid cookie - sync state to match dropdown's current visual value
        this.appState.setAgentMode(this.elements.agentModeSelect.value);
        this.updateDropdownLabel(this.elements.agentModeSelect);
      }
    }

    const savedModel = getCookie('llmModel');
    if (this.elements.modelSelect) {
      if (savedModel && Array.from(this.elements.modelSelect.options).some(option => option.value === savedModel)) {
        this.elements.modelSelect.value = savedModel;
        this.updateDropdownLabel(this.elements.modelSelect);
      } else {
        // No cookie - just update the label to match current dropdown state
        this.updateDropdownLabel(this.elements.modelSelect);
      }
    }
  }

  /**
   * Fetch available models from backend and disable unavailable ones
   */
  async fetchAvailableModels() {
    if (!this.elements.modelSelect) return;

    try {
      const response = await fetch('/api/available-models');
      if (!response.ok) {
        console.warn('Failed to fetch available models:', response.status);
        return;
      }

      const data = await response.json();
      const modelAvailability = new Map(
        data.models.map(m => [m.value, { available: m.available, reason: m.reason }])
      );

      // Track if current selection becomes unavailable
      let currentValue = this.elements.modelSelect.value;
      let needsNewSelection = false;

      // Update each option in the dropdown
      Array.from(this.elements.modelSelect.options).forEach(option => {
        const modelInfo = modelAvailability.get(option.value);

        if (modelInfo && !modelInfo.available) {
          // Disable unavailable models
          option.disabled = true;
          option.title = modelInfo.reason || 'API key not configured';

          // Add visual indicator to the label
          const originalLabel = option.getAttribute('data-original-label') || option.textContent;
          if (!originalLabel.includes('(No API Key)')) {
            option.setAttribute('data-original-label', originalLabel);
            option.textContent = `${originalLabel} (No API Key)`;
          }

          // Check if current selection is now unavailable
          if (option.value === currentValue) {
            needsNewSelection = true;
          }
        } else {
          option.disabled = false;
          option.title = '';
        }
      });

      // If current selection is unavailable, switch to first available model
      if (needsNewSelection) {
        const firstAvailable = Array.from(this.elements.modelSelect.options)
          .find(opt => !opt.disabled);

        if (firstAvailable) {
          this.elements.modelSelect.value = firstAvailable.value;
          setCookie('llmModel', firstAvailable.value, this.config.cookieExpiryDays);
          this.updateDropdownLabel(this.elements.modelSelect);
          console.info(`Switched to ${firstAvailable.value} (previous model unavailable)`);
        }
      }

      // Update the dropdown label display
      this.updateDropdownLabel(this.elements.modelSelect);

    } catch (error) {
      console.warn('Error fetching available models:', error);
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

  // ============== Activity Tracking for Lease Management ==============

  /**
   * Setup activity tracking for lease renewal
   * Tracks user interactions and syncs to backend periodically
   */
  setupActivityTracking() {
    // Debounced activity reporter
    const reportActivity = () => {
      const now = Date.now();
      if (now - this.lastActivitySync > this.ACTIVITY_SYNC_INTERVAL) {
        this.lastActivitySync = now;
        fetch('/api/update-activity', { method: 'POST' })
          .catch(err => console.warn('Activity sync failed:', err));
      }
    };

    // Track user interactions
    if (this.elements.messageInput) {
      this.elements.messageInput.addEventListener('keydown', reportActivity);
    }
    if (this.elements.messageHistory) {
      this.elements.messageHistory.addEventListener('click', reportActivity);
      this.elements.messageHistory.addEventListener('scroll', reportActivity);
    }

    // Also report activity when sending messages (in sendMessage method)
    // This is already covered by keydown on Enter

    // Fetch lease config for warning timing
    this.fetchLeaseConfig();

    // Start inactivity warning check
    this.startInactivityCheck();

    // Setup continue button handler
    const continueBtn = this.container.querySelector('[data-llamabot="continue-session"]');
    if (continueBtn) {
      continueBtn.addEventListener('click', () => {
        this.hideTimeoutWarning();
        this.lastActivitySync = 0; // Force immediate sync
        fetch('/api/update-activity', { method: 'POST' })
          .catch(err => console.warn('Continue session activity sync failed:', err));
      });
    }
  }

  /**
   * Fetch lease configuration from backend
   */
  async fetchLeaseConfig() {
    try {
      const res = await fetch('/api/lease-status');
      if (res.ok) {
        this.leaseConfig = await res.json();
        console.log('Lease config loaded:', this.leaseConfig);
      }
    } catch (err) {
      console.warn('Failed to fetch lease config:', err);
    }
  }

  /**
   * Start periodic inactivity check
   */
  startInactivityCheck() {
    // Check every 30 seconds if we should show warning
    this.inactivityCheckInterval = setInterval(() => this.checkInactivityWarning(), 30000);
  }

  /**
   * Check if we should show the inactivity warning
   */
  checkInactivityWarning() {
    // Skip if no lease config or mothership not enabled
    if (!this.leaseConfig?.lease_duration_seconds || !this.leaseConfig?.mothership_enabled) {
      return;
    }

    const leaseSeconds = this.leaseConfig.lease_duration_seconds;
    const warningThreshold = (leaseSeconds - 60) * 1000; // Show warning 1 minute before expiry
    const timeSinceSync = Date.now() - this.lastActivitySync;

    // Only show warning if user has been inactive long enough
    if (this.lastActivitySync > 0 && timeSinceSync > warningThreshold) {
      this.showTimeoutWarning();
    }
  }

  /**
   * Show the timeout warning banner
   */
  showTimeoutWarning() {
    const banner = this.container.querySelector('[data-llamabot="timeout-warning"]');
    if (banner && banner.classList.contains('hidden')) {
      banner.classList.remove('hidden');
      console.log('Showing timeout warning - user inactive');
    }
  }

  /**
   * Hide the timeout warning banner
   */
  hideTimeoutWarning() {
    const banner = this.container.querySelector('[data-llamabot="timeout-warning"]');
    if (banner) {
      banner.classList.add('hidden');
    }
  }

  /**
   * Close the tools toolbar
   */
  closeToolsToolbar() {
    if (this.elements.toolsToolbar) {
      this.elements.toolsToolbar.classList.add('hidden');
    }
    if (this.elements.toolsToggleBtn) {
      this.elements.toolsToggleBtn.classList.remove('active');
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

// Export LlamaBot as default for ES6 module imports
export default window.LlamaBot;

// Export ChatApp class and config for advanced usage
export { ChatApp, DEFAULT_CONFIG };

// Auto-initialize for backward compatibility (if body contains chat elements)
// This maintains the existing single-page app behavior
// Only auto-init if:
// 1. Chat elements exist in DOM
// 2. Not explicitly disabled via window.LlamaBot.skipAutoInit
// 3. Document is still loading (meaning loaded via <script> tag, not dynamic import)
const shouldAutoInit =
  !window.LlamaBot.skipAutoInit &&
  document.querySelector('[data-llamabot="message-history"]') &&
  (document.readyState === 'loading' || document.readyState === 'interactive');

if (shouldAutoInit) {
  // Wait for DOM to be ready before initializing
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      const legacyInstance = window.LlamaBot.create('body');
      window.chatApp = legacyInstance;
    });
  } else {
    const legacyInstance = window.LlamaBot.create('body');
    window.chatApp = legacyInstance;
  }
}
