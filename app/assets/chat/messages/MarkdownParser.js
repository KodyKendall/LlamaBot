/**
 * Markdown parsing utilities
 */

import { CONFIG } from '../config.js';

export class MarkdownParser {
  constructor() {
    // Configure marked.js
    if (typeof marked !== 'undefined') {
      marked.setOptions(CONFIG.MARKDOWN_OPTIONS);
    }
  }

  /**
   * Parse markdown text to HTML
   */
  parse(text) {
    if (!text) return '';

    try {
      // Handle array format (from Claude's LLM model)
      if (Array.isArray(text)) {
        text = text[0].text;
      }

      // Parse markdown to HTML
      let html = marked.parse(text);

      // Basic XSS prevention - remove script tags and event handlers
      html = this.sanitize(html);

      return html;
    } catch (error) {
      console.error('Markdown parsing error:', error);
      // Fallback to plain text with line breaks
      try {
        return text.replace(/\n/g, '<br>');
      } catch (fallbackError) {
        console.error('Text.replace parsing error:', fallbackError);
        return text;
      }
    }
  }

  /**
   * Sanitize HTML to prevent XSS attacks
   */
  sanitize(html) {
    // Remove script tags
    html = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');

    // Remove event handlers
    html = html.replace(/\son\w+="[^"]*"/gi, '');
    html = html.replace(/\son\w+='[^']*'/gi, '');

    return html;
  }
}
