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
    // Safety check for undefined/null/invalid input
    if (text === undefined || text === null || text === 'undefined') return '';
    if (!text) return '';

    try {
      // Handle array format (from Claude's LLM model)
      if (Array.isArray(text)) {
        text = text[0]?.text || '';
      }

      // Ensure text is a string
      text = String(text);

      // Parse markdown to HTML
      let html = marked.parse(text);

      // Basic XSS prevention - remove script tags and event handlers
      html = this.sanitize(html);

      return html;
    } catch (error) {
      console.error('Markdown parsing error:', error);
      // Fallback to plain text with line breaks
      try {
        const safeText = String(text || '');
        return safeText.replace(/\n/g, '<br>');
      } catch (fallbackError) {
        console.error('Text.replace parsing error:', fallbackError);
        return '';
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
