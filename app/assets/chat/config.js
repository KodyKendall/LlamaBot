/**
 * Application configuration constants
 */

export const CONFIG = {
  // Agent configuration
  AGENT: {
    NAME: 'rails_frontend_starter_agent',
    TYPE: 'default'
    // TYPE: 'claude_llm_model'
  },

  // Agent mode mappings
  AGENT_MODES: {
    prototype: 'rails_frontend_starter_agent',
    engineer: 'rails_agent'
  },

  // Streaming configuration
  IFRAME_REFRESH_MS: 500,

  // Scroll configuration
  SCROLL_THRESHOLD: 50, // pixels from bottom to consider "at bottom"

  // Rails iframe timeout
  RAILS_DEBUG_TIMEOUT: 250, // ms

  // Cookie settings
  COOKIE_EXPIRY_DAYS: 365,

  // Markdown configuration
  MARKDOWN_OPTIONS: {
    breaks: true,
    gfm: true,
    sanitize: false, // We'll handle XSS prevention differently
    smartLists: true,
    smartypants: true
  },

  // WebSocket reconnection
  RECONNECT_DELAY: 3000 // ms
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
 * Get WebSocket URL based on current protocol
 */
export function getWebSocketUrl() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${protocol}//${window.location.host}/ws`;
}
