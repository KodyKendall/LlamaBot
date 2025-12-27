/**
 * Clipboard formatting for chat messages
 * Adds role annotations when copying messages from chat history
 */

export class ClipboardFormatter {
  constructor(messageHistoryElement) {
    this.messageHistory = messageHistoryElement;
  }

  /**
   * Initialize clipboard event listener
   */
  init() {
    if (!this.messageHistory) return;
    this.messageHistory.addEventListener('copy', (e) => this.handleCopy(e));
  }

  /**
   * Handle copy event - format selection with role prefixes
   */
  handleCopy(event) {
    const selection = window.getSelection();
    if (!selection.rangeCount) return;

    const formattedText = this.formatSelection(selection);

    if (formattedText) {
      event.preventDefault();
      event.clipboardData.setData('text/plain', formattedText);
    }
  }

  /**
   * Format the current selection with role annotations
   */
  formatSelection(selection) {
    const range = selection.getRangeAt(0);
    const messages = this.getSelectedMessages(range);

    if (messages.length === 0) return null;

    return messages
      .map(msg => this.formatMessage(msg, selection))
      .filter(text => text.trim())
      .join('\n\n');
  }

  /**
   * Get all message elements that intersect with the selection
   */
  getSelectedMessages(range) {
    const allMessages = this.messageHistory.querySelectorAll(
      '[data-llamabot="human-message"], [data-llamabot="ai-message"], [data-llamabot="tool-message"]'
    );

    return Array.from(allMessages).filter(msg => range.intersectsNode(msg));
  }

  /**
   * Format a single message with its role prefix
   */
  formatMessage(messageElement, selection) {
    const type = messageElement.getAttribute('data-llamabot');
    const content = this.getSelectedText(messageElement, selection);

    if (!content.trim()) return '';

    switch (type) {
      case 'human-message':
        return `You Said: ${content}`;
      case 'ai-message':
        return `Leonardo Said: ${content}`;
      case 'tool-message':
        const toolName = this.extractToolName(messageElement);
        return `Tool Call (${toolName}): ${content}`;
      default:
        return content;
    }
  }

  /**
   * Get the selected text portion from a message element
   */
  getSelectedText(element, selection) {
    const range = selection.getRangeAt(0);

    // Check if element is fully contained in selection
    if (selection.containsNode(element, false)) {
      return element.textContent.trim();
    }

    // Handle partial selection
    try {
      const clonedRange = range.cloneRange();
      const elementRange = document.createRange();
      elementRange.selectNodeContents(element);

      // Adjust start boundary if selection starts after element start
      if (range.compareBoundaryPoints(Range.START_TO_START, elementRange) > 0) {
        clonedRange.setStart(range.startContainer, range.startOffset);
      } else {
        clonedRange.setStart(elementRange.startContainer, elementRange.startOffset);
      }

      // Adjust end boundary if selection ends before element end
      if (range.compareBoundaryPoints(Range.END_TO_END, elementRange) < 0) {
        clonedRange.setEnd(range.endContainer, range.endOffset);
      } else {
        clonedRange.setEnd(elementRange.endContainer, elementRange.endOffset);
      }

      return clonedRange.toString().trim();
    } catch (e) {
      // Fallback to full text content
      return element.textContent.trim();
    }
  }

  /**
   * Extract tool name from a tool message element
   */
  extractToolName(toolElement) {
    // Try compact format first
    const toolName = toolElement.querySelector('[data-llamabot="tool-compact-name"]');
    if (toolName) return toolName.textContent;

    // Fallback
    return 'Tool';
  }
}
