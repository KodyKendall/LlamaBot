/**
 * BrandGuide — an editable branding guideline that lives in the tucked-away
 * tools toolbar (the "+" menu). It grew out of the color picker: instead of a
 * one-off hex, it holds a client's brand — named colors (primary / secondary /
 * tertiary / ...), logos & icons, and free-form notes.
 *
 * Source of truth is `.leonardo/brand.json` (GET/PUT /api/brand); the backend
 * also regenerates a human-readable `.leonardo/BRAND.md` on every save so the
 * agent and the user can read the guide.
 *
 * Colors can be set three ways: the native color wheel, typing a hex, or the
 * screen eyedropper ("Pick from app") which samples any pixel on screen —
 * including inside the Rails app iframe — via the browser's EyeDropper API.
 * Each color can also be inserted into the chat input to reference it to Leo.
 */
export class BrandGuide {
  constructor() {
    this.button = null;
    this.panel = null;
    this.messageInput = null;

    this.state = { colors: [], logos: [], notes: '' };
    this.loaded = false;
    this.dirty = false;
    this._outsideClickHandler = null;

    // Hidden file input reused for every logo upload.
    this.logoInput = null;
    this._eyedropperSupported = typeof window !== 'undefined' && 'EyeDropper' in window;
  }

  /**
   * @param {HTMLElement} button - toolbar tool button that opens the panel
   * @param {HTMLElement} panel - the (hidden) brand guide panel element
   * @param {HTMLTextAreaElement} messageInput - chat input (reserved)
   * @param {(src:string, name:string)=>void} onPreviewImage - opens a full-size
   *   image lightbox (reuses the file-attachment preview modal)
   */
  init(button, panel, messageInput, onPreviewImage) {
    this.button = button;
    this.panel = panel;
    this.messageInput = messageInput;
    this.onPreviewImage = onPreviewImage;
    if (!this.button || !this.panel) return;

    // Hidden input for uploading logo images.
    this.logoInput = document.createElement('input');
    this.logoInput.type = 'file';
    this.logoInput.accept = 'image/png,image/jpeg,image/gif,image/webp,image/svg+xml,image/x-icon,image/vnd.microsoft.icon,.png,.jpg,.jpeg,.gif,.webp,.svg,.ico';
    this.logoInput.style.display = 'none';
    this.logoInput.addEventListener('change', (e) => this.onLogoChosen(e));
    this.panel.appendChild(this.logoInput);

    this.button.addEventListener('click', (e) => {
      e.stopPropagation();
      this.toggle();
    });
  }

  // ---- Data ---------------------------------------------------------------

  async loadBrand() {
    try {
      const res = await fetch('/api/brand');
      const data = await res.json();
      const brand = data.brand || {};
      this.state = {
        colors: Array.isArray(brand.colors) ? brand.colors : [],
        logos: Array.isArray(brand.logos) ? brand.logos : [],
        notes: typeof brand.notes === 'string' ? brand.notes : '',
      };
    } catch (err) {
      console.error('Failed to load brand guide:', err);
      this.state = { colors: [], logos: [], notes: '' };
    }
    this.loaded = true;
    this.dirty = false;
  }

  async save() {
    // Normalize hexes before persisting.
    const payload = {
      colors: this.state.colors.map((c) => ({
        name: (c.name || '').trim(),
        hex: this.normalizeHex(c.hex) || (c.hex || '').trim(),
      })),
      logos: this.state.logos.map((l) => ({
        name: (l.name || '').trim(),
        path: (l.path || '').trim(),
      })),
      notes: this.state.notes || '',
    };

    const saveBtn = this.panel.querySelector('[data-llamabot="brand-save-btn"]');
    if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = 'Saving…'; }

    try {
      const res = await fetch('/api/brand', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      this.dirty = false;
      this.flashSaved();
    } catch (err) {
      console.error('Failed to save brand guide:', err);
      if (saveBtn) { saveBtn.disabled = false; saveBtn.innerHTML = '<i class="fa-solid fa-triangle-exclamation"></i> Retry save'; }
    }
  }

  flashSaved() {
    const saveBtn = this.panel.querySelector('[data-llamabot="brand-save-btn"]');
    if (!saveBtn) return;
    saveBtn.disabled = false;
    saveBtn.innerHTML = '<i class="fa-solid fa-check"></i> Saved';
    setTimeout(() => {
      const btn = this.panel.querySelector('[data-llamabot="brand-save-btn"]');
      if (btn) btn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save';
    }, 1600);
  }

  markDirty() {
    this.dirty = true;
    const saveBtn = this.panel.querySelector('[data-llamabot="brand-save-btn"]');
    if (saveBtn) saveBtn.innerHTML = '<i class="fa-solid fa-floppy-disk"></i> Save';
  }

  // ---- Rendering ----------------------------------------------------------

  render() {
    this.panel.innerHTML = `
      <div class="brand-guide">
        <div class="brand-header">
          <span class="brand-title"><i class="fa-solid fa-palette"></i> Brand Guide</span>
          <div class="brand-header-actions">
            <button type="button" class="brand-save-btn" data-llamabot="brand-save-btn" title="Save brand guide">
              <i class="fa-solid fa-floppy-disk"></i> Save
            </button>
            <button type="button" class="brand-close-btn" data-llamabot="brand-close-btn" title="Close" aria-label="Close brand guide">
              <i class="fa-solid fa-xmark"></i>
            </button>
          </div>
        </div>

        <div class="brand-section">
          <div class="brand-section-title">Colors</div>
          <div class="brand-colors" data-llamabot="brand-colors"></div>
          <button type="button" class="brand-add-btn" data-llamabot="brand-add-color">
            <i class="fa-solid fa-plus"></i> Add color
          </button>
        </div>

        <div class="brand-section">
          <div class="brand-section-title">Logos &amp; Icons</div>
          <div class="brand-logos" data-llamabot="brand-logos"></div>
          <button type="button" class="brand-add-btn" data-llamabot="brand-add-logo">
            <i class="fa-solid fa-image"></i> Add logo / icon
          </button>
        </div>

        <div class="brand-section">
          <div class="brand-section-title">Notes</div>
          <textarea class="brand-notes" data-llamabot="brand-notes"
            placeholder="Fonts, voice & tone, do's and don'ts, spacing rules…"></textarea>
        </div>
      </div>
    `;
    // Re-attach the hidden logo input (innerHTML wipe removed it).
    if (this.logoInput) this.panel.appendChild(this.logoInput);

    this.panel.querySelector('[data-llamabot="brand-save-btn"]')
      .addEventListener('click', (e) => { e.stopPropagation(); this.save(); });
    this.panel.querySelector('[data-llamabot="brand-close-btn"]')
      .addEventListener('click', (e) => { e.stopPropagation(); this.close(); });
    this.panel.querySelector('[data-llamabot="brand-add-color"]')
      .addEventListener('click', (e) => { e.stopPropagation(); this.addColor(); });
    this.panel.querySelector('[data-llamabot="brand-add-logo"]')
      .addEventListener('click', (e) => { e.stopPropagation(); this.logoInput.click(); });

    const notes = this.panel.querySelector('[data-llamabot="brand-notes"]');
    notes.value = this.state.notes || '';
    notes.addEventListener('input', () => { this.state.notes = notes.value; this.markDirty(); });

    this.renderColors();
    this.renderLogos();
  }

  renderColors() {
    const container = this.panel.querySelector('[data-llamabot="brand-colors"]');
    if (!container) return;
    container.innerHTML = '';

    this.state.colors.forEach((color, i) => {
      const hex = this.normalizeHex(color.hex) || color.hex || '#000000';
      const row = document.createElement('div');
      row.className = 'brand-color-row';
      // The whole row is tinted with its own color (see .brand-color-row CSS):
      // faint wash + left accent bar. Kept in sync on every color change.
      row.style.setProperty('--row-color', hex);
      row.innerHTML = `
        <div class="brand-color-main">
          <label class="brand-swatch-wrap" title="Open color wheel">
            <span class="brand-swatch" data-role="swatch" style="background:${hex}"></span>
            <input type="color" class="brand-native" data-role="native" value="${/^#[0-9a-fA-F]{6}$/.test(hex) ? hex : '#000000'}">
          </label>
          <input type="text" class="brand-color-name" data-role="name" value="${this.escapeAttr(color.name || '')}" placeholder="Name (e.g. Primary)">
        </div>
        <input type="text" class="brand-color-hex" data-role="hex" value="${this.escapeAttr(color.hex || '')}" placeholder="#RRGGBB" maxlength="7" spellcheck="false">
        <div class="brand-row-actions">
          ${this._eyedropperSupported ? `<button type="button" class="brand-icon-btn" data-role="pick" title="Pick a color from the app (eyedropper)"><i class="fa-solid fa-eye-dropper"></i></button>` : ''}
          <button type="button" class="brand-icon-btn brand-remove" data-role="remove" title="Remove color"><i class="fa-solid fa-xmark"></i></button>
        </div>
      `;

      const swatch = row.querySelector('[data-role="swatch"]');
      const native = row.querySelector('[data-role="native"]');
      const nameInput = row.querySelector('[data-role="name"]');
      const hexInput = row.querySelector('[data-role="hex"]');

      native.addEventListener('input', () => {
        this.setColorAt(i, native.value, { row });
      });
      nameInput.addEventListener('input', () => {
        this.state.colors[i].name = nameInput.value;
        this.markDirty();
      });
      hexInput.addEventListener('input', () => {
        this.state.colors[i].hex = hexInput.value;
        this.markDirty();
        const norm = this.normalizeHex(hexInput.value);
        if (norm) {
          swatch.style.background = norm;
          native.value = norm;
          row.style.setProperty('--row-color', norm);
        }
      });
      hexInput.addEventListener('blur', () => {
        const norm = this.normalizeHex(hexInput.value);
        if (norm) { hexInput.value = norm; this.state.colors[i].hex = norm; }
      });

      const pickBtn = row.querySelector('[data-role="pick"]');
      if (pickBtn) pickBtn.addEventListener('click', (e) => { e.stopPropagation(); this.pickFromApp(i, row); });
      row.querySelector('[data-role="remove"]').addEventListener('click', (e) => {
        e.stopPropagation(); this.removeColor(i);
      });

      container.appendChild(row);
    });
  }

  renderLogos() {
    const container = this.panel.querySelector('[data-llamabot="brand-logos"]');
    if (!container) return;
    container.innerHTML = '';

    if (this.state.logos.length === 0) {
      const empty = document.createElement('div');
      empty.className = 'brand-empty';
      empty.textContent = 'No logos yet — add your logo, wordmark, or favicon.';
      container.appendChild(empty);
      return;
    }

    this.state.logos.forEach((logo, i) => {
      const previewUrl = `/api/uploaded-files/preview?path=${encodeURIComponent(logo.path)}`;
      const row = document.createElement('div');
      row.className = 'brand-logo-row';
      row.innerHTML = `
        <button type="button" class="brand-logo-thumb" data-role="thumb" title="Click to view full image">
          <img src="${previewUrl}" alt="" loading="lazy">
          <span class="brand-logo-zoom"><i class="fa-solid fa-magnifying-glass-plus"></i></span>
        </button>
        <input type="text" class="brand-logo-name" data-role="name" value="${this.escapeAttr(logo.name || '')}" placeholder="Label (e.g. Primary logo)">
        <button type="button" class="brand-icon-btn brand-remove" data-role="remove" title="Remove logo"><i class="fa-solid fa-xmark"></i></button>
      `;
      row.querySelector('[data-role="thumb"]').addEventListener('click', (e) => {
        e.stopPropagation();
        if (this.onPreviewImage) this.onPreviewImage(previewUrl, logo.name || logo.path);
      });
      row.querySelector('[data-role="name"]').addEventListener('input', (e) => {
        this.state.logos[i].name = e.target.value;
        this.markDirty();
      });
      row.querySelector('[data-role="remove"]').addEventListener('click', (e) => {
        e.stopPropagation(); this.removeLogo(i);
      });
      container.appendChild(row);
    });
  }

  // ---- Color operations ---------------------------------------------------

  addColor() {
    // Default to a real brand hex (not black) so the swatch reads as a color.
    this.state.colors.push({ name: '', hex: '#8B5CF6' });
    this.markDirty();
    this.renderColors();
  }

  removeColor(i) {
    const c = this.state.colors[i] || {};
    const label = (c.name || '').trim() || (c.hex || '').trim() || 'this color';
    if (!window.confirm(`Remove ${label} from the brand guide?`)) return;
    this.state.colors.splice(i, 1);
    this.markDirty();
    this.renderColors();
  }

  setColorAt(i, value, { row } = {}) {
    const norm = this.normalizeHex(value) || value;
    this.state.colors[i].hex = norm;
    this.markDirty();
    if (row) {
      const swatch = row.querySelector('[data-role="swatch"]');
      const hexInput = row.querySelector('[data-role="hex"]');
      const native = row.querySelector('[data-role="native"]');
      if (swatch) swatch.style.background = norm;
      if (hexInput) hexInput.value = norm;
      if (native && /^#[0-9a-fA-F]{6}$/.test(norm)) native.value = norm;
      row.style.setProperty('--row-color', norm);
    }
  }

  async pickFromApp(i, row) {
    if (!this._eyedropperSupported) return;
    try {
      // eslint-disable-next-line no-undef
      const eyeDropper = new EyeDropper();
      const result = await eyeDropper.open();
      if (result && result.sRGBHex) {
        this.setColorAt(i, result.sRGBHex, { row });
      }
    } catch (err) {
      // User pressed Esc / cancelled — nothing to do.
      if (err && err.name !== 'AbortError') console.error('Eyedropper failed:', err);
    }
  }

  // ---- Logo operations ----------------------------------------------------

  async onLogoChosen(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = '';
    if (!file) return;

    const container = this.panel.querySelector('[data-llamabot="brand-logos"]');
    const addBtn = this.panel.querySelector('[data-llamabot="brand-add-logo"]');
    if (addBtn) { addBtn.disabled = true; addBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Uploading…'; }

    try {
      const form = new FormData();
      form.append('file', file);
      const res = await fetch('/api/upload-to-assets', { method: 'POST', body: form });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const label = file.name.replace(/\.[^.]+$/, '');
      this.state.logos.push({ name: label, path: data.path });
      this.markDirty();
      this.renderLogos();
    } catch (err) {
      console.error('Logo upload failed:', err);
      if (container) {
        const warn = document.createElement('div');
        warn.className = 'brand-empty';
        warn.textContent = 'Upload failed — please try again.';
        container.appendChild(warn);
      }
    } finally {
      if (addBtn) { addBtn.disabled = false; addBtn.innerHTML = '<i class="fa-solid fa-image"></i> Add logo / icon'; }
    }
  }

  removeLogo(i) {
    const lg = this.state.logos[i] || {};
    const label = (lg.name || '').trim() || 'this logo';
    if (!window.confirm(`Remove ${label} from the brand guide?`)) return;
    // Only removes it from the guide; the uploaded asset itself is left in place.
    this.state.logos.splice(i, 1);
    this.markDirty();
    this.renderLogos();
  }

  // ---- Insert into chat (kept from the original color picker) --------------

  insertColor(hexRaw) {
    const hex = this.normalizeHex(hexRaw);
    if (!hex || !this.messageInput) return;
    const input = this.messageInput;
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    const before = input.value.slice(0, start);
    const after = input.value.slice(end);
    const needsLeadingSpace = before.length > 0 && !/\s$/.test(before);
    const text = (needsLeadingSpace ? ' ' : '') + hex + ' ';
    input.value = before + text + after;
    const caret = before.length + text.length;
    input.setSelectionRange(caret, caret);
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.focus();
    this.close();
  }

  // ---- Helpers ------------------------------------------------------------

  /** "#abc" | "abc" | "#aabbcc" | "aabbcc" -> "#AABBCC", else null. */
  normalizeHex(raw) {
    if (!raw) return null;
    let v = String(raw).trim().replace(/^#/, '');
    if (/^[0-9a-fA-F]{3}$/.test(v)) v = v.split('').map((c) => c + c).join('');
    if (/^[0-9a-fA-F]{6}$/.test(v)) return '#' + v.toUpperCase();
    return null;
  }

  escapeAttr(s) {
    return String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  // ---- Open/close ---------------------------------------------------------

  async toggle() {
    if (this.panel.classList.contains('hidden')) {
      await this.open();
    } else {
      this.close();
    }
  }

  async open() {
    if (!this.loaded) {
      this.panel.classList.remove('hidden');
      this.panel.innerHTML = '<div class="brand-guide"><div class="brand-empty">Loading brand guide…</div></div>';
      await this.loadBrand();
    }
    this.render();
    this.panel.classList.remove('hidden');
    this.button.classList.add('active');

    this._outsideClickHandler = (e) => {
      if (!this.panel.contains(e.target) && !this.button.contains(e.target)) {
        this.close();
      }
    };
    setTimeout(() => document.addEventListener('click', this._outsideClickHandler), 0);
  }

  close() {
    // Persist any pending edits so nothing is lost on dismiss.
    if (this.dirty) this.save();
    this.panel.classList.add('hidden');
    this.button.classList.remove('active');
    if (this._outsideClickHandler) {
      document.removeEventListener('click', this._outsideClickHandler);
      this._outsideClickHandler = null;
    }
  }
}
