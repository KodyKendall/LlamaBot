/**
 * SVG icon utilities for tool messages
 * Provides clean, minimal icons instead of emojis
 */

export const ToolIcons = {
  /**
   * Get SVG icon for a tool name
   */
  getIcon(toolName) {
    const iconMap = {
      'edit_file': this.editIcon(),
      'write_file': this.fileIcon(),
      'read_file': this.fileIcon(),
      'search': this.searchIcon(),
      'bash': this.terminalIcon(),
      'terminal': this.terminalIcon(),
      'git': this.gitIcon(),
      'write_todos': this.checklistIcon(),
      'default': this.toolIcon(),
    };

    return iconMap[toolName] || iconMap['default'];
  },

  /**
   * Status icons
   */
  successIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><path d="M5 8l2 2 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  },

  errorIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="7" stroke="currentColor" stroke-width="1.5"/><path d="M6 6l4 4M10 6l-4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  },

  pendingIcon() {
    return `<svg data-llamabot="tool-icon" data-state="spinning" viewBox="0 0 16 16" fill="none"><circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" opacity="0.25"/><path d="M8 2a6 6 0 0 1 6 6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  },

  /**
   * Tool-specific icons
   */
  editIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><path d="M11.5 1.5l3 3-8 8H3.5v-3l8-8z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M9.5 3.5l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  },

  fileIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><path d="M3 2h7l3 3v9H3V2z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M10 2v3h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  },

  searchIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><circle cx="7" cy="7" r="4.5" stroke="currentColor" stroke-width="1.5"/><path d="M10 10l3.5 3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  },

  terminalIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><rect x="2" y="3" width="12" height="10" rx="1.5" stroke="currentColor" stroke-width="1.5"/><path d="M4.5 6.5l2 1.5-2 1.5M8 9.5h3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
  },

  gitIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><circle cx="4" cy="8" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="4" r="2" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="2" stroke="currentColor" stroke-width="1.5"/><path d="M6 8h4M12 6v4" stroke="currentColor" stroke-width="1.5"/></svg>`;
  },

  checklistIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><path d="M3 2h10v12H3V2z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M5 5h6M5 8h6M5 11h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  },

  toolIcon() {
    return `<svg data-llamabot="tool-icon" viewBox="0 0 16 16" fill="none"><path d="M10 2l4 4-6 6H4v-4l6-6z" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M8 4l4 4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
  }
};
