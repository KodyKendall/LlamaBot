/**
 * Agent mode helpers
 */

/**
 * True when `mode` is a per-instance custom agent mode (agent_modes.json),
 * injected by the backend as window.LLAMABOT_CUSTOM_AGENT_MODES.
 * Custom modes use the simplified UX: tool calls and sub-agent content are
 * hidden, same as beginner/engineer mode.
 */
export function isCustomAgentMode(mode) {
  const modes = Array.isArray(window.LLAMABOT_CUSTOM_AGENT_MODES)
    ? window.LLAMABOT_CUSTOM_AGENT_MODES : [];
  return modes.some(m => m && m.key === mode);
}
