/**
 * Default configuration for LlamaBot Client
 * These can be overridden when creating an instance
 */

export const DEFAULT_CONFIG = {
  // WebSocket configuration
  websocketUrl: null, // Auto-detect if null

  // ActionCable configuration (alternative to websocketUrl)
  // Use this when connecting through Rails ActionCable instead of direct WebSocket
  // Example:
  //   actionCable: {
  //     consumer: LlamaBotRails.cable,
  //     channel: 'LlamaBotRails::ChatChannel',
  //     session_id: 'unique-session-id'
  //   }
  actionCable: null,

  // Agent configuration
  agent: {
    name: 'rails_frontend_starter_agent',
    type: 'default'
  },

  // Agent mode mappings
  agentModes: {
    prototype: 'rails_frontend_starter_agent',
    engineer: 'rails_agent',
    ai_builder: 'rails_ai_builder_agent',
    testing: 'rails_testing_agent'
  },

  // Streaming configuration
  iframeRefreshMs: 500,

  // Scroll configuration
  scrollThreshold: 50, // pixels from bottom to consider "at bottom"

  // Rails iframe timeout
  railsDebugTimeout: 250, // ms

  // Cookie settings
  cookieExpiryDays: 365,

  // Markdown configuration
  markdownOptions: {
    breaks: true,
    gfm: true,
    sanitize: false, // We'll handle XSS prevention differently
    smartLists: true,
    smartypants: true
  },

  // WebSocket reconnection
  reconnectDelay: 3000, // ms

  // Custom renderers (can be overridden)
  toolRenderers: {},
  messageRenderers: {},

  // Custom CSS classes for styling (optional - for Tailwind/Bootstrap integration)
  cssClasses: {
    humanMessage: '',  // e.g., 'bg-blue-100 p-3 rounded-lg'
    aiMessage: '',     // e.g., 'bg-gray-100 p-3 rounded-lg'
    errorMessage: '',  // e.g., 'bg-red-100 p-3 rounded-lg text-red-800'
    queuedMessage: '', // e.g., 'bg-yellow-50 p-3 rounded-lg'
    connectionStatusConnected: '',    // e.g., 'bg-green-400'
    connectionStatusDisconnected: ''  // e.g., 'bg-red-400'
  },

  // Callbacks (can be overridden)
  onMessageReceived: null,
  onToolResult: null,
  onError: null
};

/**
 * Get Rails URL based on current protocol
 */
export function getRailsUrl() {
  if (window.location.protocol === 'https:') {
    return 'https://rails-' + window.location.host;
  }
  return 'http://localhost:3000';
}

/**
 * Get VS Code URL based on current protocol
 */
export function getVSCodeUrl() {
  if (window.location.protocol === 'https:') {
    return 'https://vscode-' + window.location.host;
  }
  return 'http://localhost:8443';
}

/**
 * Get WebSocket URL based on current protocol
 */
export function getWebSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}
