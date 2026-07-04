/**
 * ColorAttach — a one-gesture standalone color selector in the tools toolbar.
 *
 * Click the toolbar button → the screen eyedropper opens immediately → click
 * any pixel on the page (including inside the app iframe) → the sampled color is
 * attached to the next message as a removable chip (like an image attachment).
 * No popover, no extra "attach" click. On send, the chat integration appends a
 * short "the user selected this color" block and clears the chips.
 *
 * The eyedropper uses the browser EyeDropper API (Chromium). On browsers that
 * lack it (Firefox/Safari) we fall back to the native color picker so the
 * button still works.
 */
export class ColorAttach {
  constructor() {
    this.button = null;
    this.messageInput = null;
    this.colors = [];            // attached hex strings, in order
    this.chipContainer = null;
    this.fallbackInput = null;   // native <input type="color"> (only if no EyeDropper)
    this._eyedropperSupported = typeof window !== 'undefined' && 'EyeDropper' in window;
  }

  /**
   * @param {HTMLElement} button - toolbar button that opens the eyedropper
   * @param {HTMLTextAreaElement} messageInput - chat input (chips render above it)
   */
  init(button, messageInput) {
    this.button = button;
    this.messageInput = messageInput;
    if (!this.button) return;

    this.button.addEventListener('click', (e) => {
      e.stopPropagation();
      // Call synchronously inside the click so the browser treats it as a user
      // gesture (required to open the eyedropper).
      this.pick();
    });
  }

  async pick() {
    if (this._eyedropperSupported) {
      try {
        // eslint-disable-next-line no-undef
        const result = await new EyeDropper().open();
        if (result && result.sRGBHex) {
          const hex = this.normalizeHex(result.sRGBHex);
          if (hex) this.attach(hex);
        }
      } catch (err) {
        // User pressed Esc / cancelled — nothing to attach.
        if (err && err.name !== 'AbortError') console.error('Eyedropper failed:', err);
      }
      return;
    }
    // Fallback for browsers without the EyeDropper API.
    this.openFallbackPicker();
  }

  openFallbackPicker() {
    if (!this.fallbackInput) {
      this.fallbackInput = document.createElement('input');
      this.fallbackInput.type = 'color';
      this.fallbackInput.setAttribute('aria-hidden', 'true');
      this.fallbackInput.tabIndex = -1;
      Object.assign(this.fallbackInput.style, {
        position: 'fixed', left: '0', bottom: '0', width: '1px', height: '1px',
        opacity: '0', border: 'none', padding: '0',
      });
      this.fallbackInput.addEventListener('change', () => {
        const hex = this.normalizeHex(this.fallbackInput.value);
        if (hex) this.attach(hex);
      });
      document.body.appendChild(this.fallbackInput);
    }
    this.fallbackInput.click();
  }

  attach(hex) {
    if (!this.colors.includes(hex)) this.colors.push(hex);
    this.renderChips();
    if (this.messageInput) this.messageInput.focus();
  }

  renderChips() {
    // Ensure a chip container sits directly before the message input.
    if (!this.chipContainer || !this.chipContainer.isConnected) {
      this.chipContainer = document.createElement('div');
      this.chipContainer.className = 'color-attach-chips';
      this.messageInput.parentElement.insertBefore(this.chipContainer, this.messageInput);
    }
    this.chipContainer.innerHTML = '';

    if (this.colors.length === 0) {
      this.chipContainer.remove();
      this.chipContainer = null;
      return;
    }

    this.colors.forEach((hex, i) => {
      const chip = document.createElement('div');
      chip.className = 'color-attach-chip';
      chip.innerHTML = `
        <span class="color-attach-chip-swatch" style="background:${hex}"></span>
        <span class="color-attach-chip-hex">${hex}</span>
        <button type="button" class="color-attach-chip-close" title="Remove color">&times;</button>
      `;
      chip.querySelector('.color-attach-chip-close').addEventListener('click', () => this.removeChip(i));
      this.chipContainer.appendChild(chip);
    });
  }

  removeChip(i) {
    this.colors.splice(i, 1);
    this.renderChips();
  }

  /** Attached hex strings, for the send integration. */
  getColors() {
    return this.colors;
  }

  clear() {
    this.colors = [];
    this.renderChips();
  }

  /** "#abc" | "abc" | "#aabbcc" | "aabbcc" -> "#AABBCC", else null. */
  normalizeHex(raw) {
    if (!raw) return null;
    let v = String(raw).trim().replace(/^#/, '');
    if (/^[0-9a-fA-F]{3}$/.test(v)) v = v.split('').map((c) => c + c).join('');
    if (/^[0-9a-fA-F]{6}$/.test(v)) return '#' + v.toUpperCase();
    return null;
  }
}
