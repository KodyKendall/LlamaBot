/**
 * Markdown parsing utilities
 */

import { DEFAULT_CONFIG } from '../config.js';

export class MarkdownParser {
  constructor() {
    // Configure marked.js with custom renderer for code blocks
    if (typeof marked !== 'undefined') {
      const renderer = new marked.Renderer();
      const self = this;

      // Custom code block renderer with copy button
      // Note: marked.js v4+ passes an object { text, lang, escaped } instead of separate params
      renderer.code = function(codeObj, language) {
        // Handle both old format (string, language) and new format ({ text, lang })
        let codeText, lang;
        if (typeof codeObj === 'object' && codeObj !== null) {
          codeText = codeObj.text || '';
          lang = codeObj.lang || '';
        } else {
          codeText = codeObj || '';
          lang = language || '';
        }

        const escaped = self.escapeHtmlForCode(codeText);
        const langClass = lang ? ` class="language-${lang}"` : '';
        const langLabel = lang ? `<span class="code-block-lang">${lang}</span>` : '';
        return `
          <div class="code-block-container">
            ${langLabel}
            <button class="code-block-copy-btn" data-llamabot="code-copy-btn" title="Copy code">
              <i class="fa-regular fa-copy"></i>
            </button>
            <pre><code${langClass}>${escaped}</code></pre>
          </div>
        `;
      };

      marked.setOptions({
        ...DEFAULT_CONFIG.markdownOptions,
        renderer: renderer
      });
    }
  }

  /**
   * Escape HTML for code blocks
   */
  escapeHtmlForCode(text) {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
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
