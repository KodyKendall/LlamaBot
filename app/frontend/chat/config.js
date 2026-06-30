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
    name: 'rails_beginner_agent',
    type: 'default'
  },

  // Agent mode mappings
  agentModes: {
    engineer: 'rails_agent',
    ai_builder: 'rails_ai_builder_agent',
    testing: 'rails_testing_agent',
    ticket: 'rails_ticket_mode_agent',
    user: 'rails_user_mode_agent',
    beginner: 'rails_beginner_agent',
    pyxl: 'pyxl_agent',
    plan: 'rails_plan_mode_agent',
    engineer_plan: 'rails_engineer_plan_mode_agent',
    ticket_plan: 'rails_ticket_plan_mode_agent'
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
  // Keep retrying long enough to ride out a full container recreate (e.g. the
  // "Update now" flow restarts llamabot, which can take 30-60s to come back).
  // At 3s/attempt this covers ~90s so the socket self-heals instead of going
  // permanently red and stranding a user who chose to keep working.
  maxReconnectAttempts: 30,

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
 * Resolve the active agent-mode -> agent_name map.
 *
 * Built-in modes (DEFAULT_CONFIG.agentModes) are always present and always win.
 * Per-instance custom modes injected by the backend as
 * window.LLAMABOT_CUSTOM_AGENT_MODES ([{ key, agent_name, ... }]) are merged in
 * underneath, so an instance can add its own agent modes without an image
 * rebuild. With no custom modes injected this returns the built-in map verbatim
 * (fully back-compatible).
 */
export function getAgentModeMap() {
  const custom = (typeof window !== 'undefined' && Array.isArray(window.LLAMABOT_CUSTOM_AGENT_MODES))
    ? window.LLAMABOT_CUSTOM_AGENT_MODES
    : [];
  const map = {};
  for (const m of custom) {
    if (m && typeof m.key === 'string' && typeof m.agent_name === 'string') {
      map[m.key] = m.agent_name;
    }
  }
  // Built-ins win on key collision.
  return Object.assign(map, DEFAULT_CONFIG.agentModes);
}

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
 * Get Tickets URL based on current protocol
 */
export function getTicketsUrl() {
  return getRailsUrl() + '/llama_bot/tickets';
}

/**
 * Get Feedback URL based on current protocol
 */
export function getFeedbackUrl() {
  return getRailsUrl() + '/llama_bot/feedback';
}

/**
 * Get WebSocket URL based on current protocol
 */
export function getWebSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}
