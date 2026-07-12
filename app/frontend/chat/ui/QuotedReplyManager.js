/**
 * Quoted Reply Manager
 *
 * Lets the user "reply" to a specific chat message. The quoted message is shown
 * as a preview card above the input (like a selected element or attached image),
 * and is injected into the outgoing message on send so Leo can see which message
 * the user is replying to and quote it back.
 *
 * Only one quote is active at a time — replying to another message replaces it.
 */
export class QuotedReplyManager {
  constructor() {
    this.messageInput = null;
    this.quote = null; // { role: 'assistant' | 'user', content: string }
    this.previewContainer = null;
  }

  /**
   * @param {HTMLElement} messageInput - The message input textarea
   */
  init(messageInput) {
    this.messageInput = messageInput;
  }

  /**
   * Set (or replace) the message being replied to and render the preview.
   * @param {{role: string, content: string}} quote
   */
  setQuote({ role, content }) {
    if (!content || !content.trim()) return;
    this.quote = { role: role === 'assistant' ? 'assistant' : 'user', content: content.trim() };
    this.renderPreview();

    // Focus the input so the user can type their reply immediately.
    if (this.messageInput) {
      this.messageInput.focus();
      this.messageInput.dispatchEvent(new Event('input', { bubbles: true }));
    }
  }

  /**
   * The currently active quote, or null.
   */
  getQuote() {
    return this.quote;
  }

  /**
   * Wrap the quoted message in a block the model can recognize, so it knows the
   * user is replying to that specific earlier message.
   * Returns '' when there's no active quote.
   */
  buildMessageBlock() {
    if (!this.quote) return '';
    const speaker = this.quote.role === 'assistant' ? 'Leonardo' : 'the user';
    return `<QUOTED_REPLY>\nThe user is replying to this specific earlier message from ${speaker}:\n"""\n${this.quote.content}\n"""\n</QUOTED_REPLY>`;
  }

  /**
   * Render the reply preview card directly above the message input.
   */
  renderPreview() {
    if (!this.messageInput) return;

    if (!this.previewContainer || !this.previewContainer.isConnected) {
      this.previewContainer = document.createElement('div');
      this.previewContainer.className = 'quoted-reply-container';
      this.messageInput.parentElement.insertBefore(this.previewContainer, this.messageInput);
    }

    this.previewContainer.innerHTML = '';

    if (!this.quote) {
      this.previewContainer.remove();
      this.previewContainer = null;
      return;
    }

    const label = this.quote.role === 'assistant' ? 'Replying to Leonardo' : 'Replying to your message';

    const badge = document.createElement('div');
    badge.className = 'quoted-reply-badge';

    const body = document.createElement('div');
    body.className = 'quoted-reply-body';

    const heading = document.createElement('div');
    heading.className = 'quoted-reply-label';
    heading.innerHTML = `<i class="fa-solid fa-reply"></i> ${label}`;

    const text = document.createElement('div');
    text.className = 'quoted-reply-text';
    text.textContent = this.quote.content;
    text.title = this.quote.content;

    body.appendChild(heading);
    body.appendChild(text);

    const closeBtn = document.createElement('button');
    closeBtn.className = 'quoted-reply-close';
    closeBtn.title = 'Cancel reply';
    closeBtn.setAttribute('aria-label', 'Cancel reply');
    closeBtn.textContent = '×';
    closeBtn.addEventListener('click', () => this.clear());

    badge.appendChild(body);
    badge.appendChild(closeBtn);
    this.previewContainer.appendChild(badge);
  }

  /**
   * Clear the active quote (after send, or when the user cancels).
   */
  clear() {
    this.quote = null;
    this.renderPreview();
  }
}
