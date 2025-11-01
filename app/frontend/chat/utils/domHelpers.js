/**
 * DOM utility functions
 */

/**
 * Escape HTML to prevent XSS
 */
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Generate a unique ID for elements
 */
export function generateUniqueId(prefix = 'id') {
  return `${prefix}_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`;
}

/**
 * Clean escaped HTML characters
 */
export function cleanEscapedHtml(html) {
  return html
    .replace(/\\n/g, '\n')
    .replace(/\\"/g, '"')
    .replace(/\\t/g, '\t')
    .replace(/\\r/g, '\r');
}

/**
 * Create a DOM element with attributes
 */
export function createElement(tag, attributes = {}, children = []) {
  const element = document.createElement(tag);

  Object.entries(attributes).forEach(([key, value]) => {
    if (key === 'className') {
      element.className = value;
    } else if (key === 'innerHTML') {
      element.innerHTML = value;
    } else if (key === 'textContent') {
      element.textContent = value;
    } else if (key.startsWith('on')) {
      element.addEventListener(key.substring(2).toLowerCase(), value);
    } else {
      element.setAttribute(key, value);
    }
  });

  children.forEach(child => {
    if (typeof child === 'string') {
      element.appendChild(document.createTextNode(child));
    } else if (child instanceof Node) {
      element.appendChild(child);
    }
  });

  return element;
}
