/**
 * SlashCommandManager
 *
 * Manages the slash command dropdown in the chat input.
 * Shows available commands when user types "/" and executes them
 * with a confirmation dialog.
 */

/**
 * Find a "/cookbook" search at the caret. The trigger is a "/cookbook" token at a
 * word boundary ANYWHERE in the message — mid-sentence counts — and everything
 * typed after it up to the caret is the search query. Unlike every other slash
 * command this one stays open once a space is typed: the space starts the search.
 *
 * @param {string} value   the whole composer contents
 * @param {number} [caret] cursor offset; defaults to the end of the text
 * @returns {{query: string, start: number, end: number}|null} start/end delimit
 *          the text a picked recipe replaces — the rest of the message is left
 *          exactly as the user typed it.
 */
export function findCookbookTrigger(value, caret) {
  const text = String(value == null ? '' : value);
  const pos = (typeof caret === 'number' && caret >= 0 && caret <= text.length)
    ? caret
    : text.length;
  // [ \t] rather than \s: a newline ends the search instead of swallowing the
  // next paragraph into the query.
  const match = /(^|\s)\/cookbook(?:[ \t]+([^\n]*))?$/i.exec(text.slice(0, pos));
  if (!match) return null;
  return {
    query: (match[2] || '').trim(),
    start: match.index + match[1].length,
    end: pos,
  };
}

/**
 * Find the slash token the user is typing at the caret — the menu opens on a "/"
 * at any word boundary, not just at the start of the message, so you can reach for
 * a command mid-thought. "app/frontend" is a path, not a menu.
 *
 * A bare "/" only opens the whole menu when it starts the message: mid-sentence a
 * stray slash ("what is 5 / 2") would otherwise leave a menu open that the next
 * Enter fires.
 *
 * @returns {{kind: 'cookbook'|'command', query: string, start: number, end: number}|null}
 *          start/end delimit the text a pick replaces.
 */
export function findSlashTrigger(value, caret) {
  const cookbook = findCookbookTrigger(value, caret);
  if (cookbook) return { kind: 'cookbook', ...cookbook };

  const text = String(value == null ? '' : value);
  const pos = (typeof caret === 'number' && caret >= 0 && caret <= text.length)
    ? caret
    : text.length;
  const match = /(^|\s)\/([A-Za-z0-9_-]*)$/.exec(text.slice(0, pos));
  if (!match) return null;

  const start = match.index + match[1].length;
  const query = match[2].toLowerCase();
  if (query === '' && start !== 0) return null;
  return { kind: 'command', query, start, end: pos };
}

/**
 * Filter cookbook recipes by a free-text query. Every whitespace-separated word
 * must appear somewhere in the recipe (title, summary, category, tags or slug),
 * so "pdf export" narrows rather than widens. Title matches rank above the rest
 * — searching "auth" should lead with recipes about auth, not ones that merely
 * mention it in a summary.
 */
export function filterCookbookGuides(guides, query) {
  const list = Array.isArray(guides) ? guides : [];
  const words = String(query || '').toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) {
    // Same rule with no query typed: the owner's recipes head the list.
    return [...list].sort((a, b) => (a.personal ? 0 : 1) - (b.personal ? 0 : 1));
  }

  const haystack = (g) => [
    g.title, g.summary, g.category, g.slug, ...(g.tags || []),
  ].join(' ').toLowerCase();

  const scored = [];
  list.forEach((g, index) => {
    const hay = haystack(g);
    if (!words.every(w => hay.includes(w))) return;
    const title = String(g.title || '').toLowerCase();
    const tags = (g.tags || []).join(' ').toLowerCase();
    const category = String(g.category || '').toLowerCase();
    let rank = 3;
    if (words.every(w => title.includes(w))) rank = 0;
    else if (words.every(w => tags.includes(w))) rank = 1;
    else if (words.every(w => category.includes(w))) rank = 2;
    // The user's OWN recipes come first within their match quality. They published them
    // from one of their boxes precisely so they could reuse them here, so when they and a
    // fleet guide match equally well, theirs is the one they meant.
    scored.push({ g, rank, personal: g.personal ? 0 : 1, index });
  });

  // Relevance first, ownership as the tiebreaker. The ticket asked for personal recipes
  // "above fleet guides"; sorting on ownership BEFORE match quality also buries a fleet
  // guide that is a clearly better answer to what the user typed, which is a worse search
  // than the one we have. Equal match quality -> the user's own recipe wins, which is the
  // case the request was actually about.
  scored.sort((a, b) => (a.rank - b.rank) || (a.personal - b.personal) || (a.index - b.index));
  return scored.map(s => s.g);
}

/** The machine-readable URL for a recipe — what the agent curls for the guide. */
export function cookbookJsonUrl(guide) {
  const page = guide.url || `https://llamapress.ai/cookbook/${guide.slug}`;
  return `${page}.json`;
}

/**
 * The short reference a picked recipe drops into the composer. It replaces only
 * the "/cookbook …" the user typed, so it can land mid-sentence without wiping
 * the thought already in the box. The agent prompts recognize this shape and
 * curl the URL for the full recipe.
 */
export function cookbookMention(guide) {
  return `@cookbook:${guide.slug} (${cookbookJsonUrl(guide)})`;
}

export class SlashCommandManager {
  constructor(container = null) {
    this.container = container || document;
    this.messageInput = null;
    this.dropdown = null;
    this.commands = [];
    this.hostCommands = [];   // privileged host slash commands (/api/slash-commands)
    this.skillCommands = [];  // filesystem Agent Skills (/api/skills), shown to all users
    this.cookbookGuides = null;   // published recipes (/api/cookbook), lazily fetched
    this.cookbookFetchedAt = 0;
    this.cookbookLoading = false;
    this.slashRange = null;       // the "/…" span a pick writes over
    this.isOpen = false;
    this.selectedIndex = -1;
    this.confirmModal = null;
    this.historyModal = null;
    this.outputModal = null;
    this.chatApp = null; // Reference to ChatApp for adding messages
  }

  /**
   * Initialize the slash command manager
   * @param {HTMLElement} messageInput - The chat textarea
   * @param {Object} chatApp - Reference to the ChatApp instance (optional)
   */
  init(messageInput, chatApp = null) {
    this.messageInput = messageInput;
    this.chatApp = chatApp;

    if (!this.messageInput) {
      console.warn('Message input not found for SlashCommandManager');
      return;
    }

    this.createDropdown();
    this.createConfirmModal();
    this.createHistoryModal();
    this.createOutputModal();
    this.attachEventListeners();
    this.fetchCommands();
  }

  /**
   * Create dropdown element
   */
  createDropdown() {
    this.dropdown = document.createElement('div');
    this.dropdown.className = 'slash-command-dropdown hidden';
    this.dropdown.setAttribute('data-llamabot', 'slash-command-dropdown');

    // Insert into the input area container
    const inputArea = this.messageInput.closest('.input-area') || this.messageInput.parentElement;
    inputArea.style.position = 'relative'; // Ensure positioning context
    inputArea.appendChild(this.dropdown);
  }

  /**
   * Create confirmation modal
   */
  createConfirmModal() {
    this.confirmModal = document.createElement('div');
    this.confirmModal.className = 'slash-command-modal hidden';
    this.confirmModal.innerHTML = `
      <div class="slash-command-modal-content">
        <div class="modal-header">
          <i class="fa-solid fa-terminal"></i>
          <span class="modal-title">Execute Command</span>
        </div>
        <div class="modal-body">
          <p class="modal-command"></p>
          <p class="modal-message"></p>
        </div>
        <div class="modal-actions">
          <button class="modal-cancel">Cancel</button>
          <button class="modal-confirm">Execute</button>
        </div>
      </div>
    `;

    document.body.appendChild(this.confirmModal);

    // Event listeners for modal
    this.confirmModal.querySelector('.modal-cancel').addEventListener('click', () => {
      this.hideConfirmModal();
    });

    this.confirmModal.addEventListener('click', (e) => {
      if (e.target === this.confirmModal) {
        this.hideConfirmModal();
      }
    });

    // Handle Escape key to close modal
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.confirmModal.classList.contains('hidden')) {
        this.hideConfirmModal();
      }
    });
  }

  /**
   * Create command history modal
   */
  createHistoryModal() {
    this.historyModal = document.createElement('div');
    this.historyModal.className = 'command-history-modal hidden';
    this.historyModal.innerHTML = `
      <div class="command-history-modal-content">
        <div class="modal-header">
          <div class="modal-header-left">
            <i class="fa-solid fa-clock-rotate-left"></i>
            <span class="modal-title">Command History</span>
          </div>
          <button class="modal-close"><i class="fa-solid fa-times"></i></button>
        </div>
        <div class="command-history-list">
          <div class="history-loading">Loading...</div>
        </div>
      </div>
    `;

    document.body.appendChild(this.historyModal);

    // Close button handler
    this.historyModal.querySelector('.modal-close').addEventListener('click', () => {
      this.hideHistoryModal();
    });

    // Close on backdrop click
    this.historyModal.addEventListener('click', (e) => {
      if (e.target === this.historyModal) {
        this.hideHistoryModal();
      }
    });

    // Handle Escape key to close modal
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.historyModal.classList.contains('hidden')) {
        this.hideHistoryModal();
      }
    });
  }

  /**
   * Show command history modal
   */
  async showHistoryModal() {
    this.historyModal.classList.remove('hidden');
    const listContainer = this.historyModal.querySelector('.command-history-list');
    listContainer.innerHTML = '<div class="history-loading"><i class="fa-solid fa-spinner fa-spin"></i> Loading history...</div>';

    try {
      const response = await fetch('/api/slash-commands/history');
      if (!response.ok) {
        throw new Error('Failed to fetch history');
      }

      const history = await response.json();

      if (history.length === 0) {
        listContainer.innerHTML = '<div class="history-empty">No commands have been executed yet.</div>';
        return;
      }

      listContainer.innerHTML = history.map((entry, index) => `
        <div class="command-history-item ${entry.success ? 'success' : 'error'}" data-index="${index}">
          <div class="history-item-header">
            <div class="history-item-left">
              <span class="history-status-icon">
                ${entry.success
                  ? '<i class="fa-solid fa-check-circle"></i>'
                  : '<i class="fa-solid fa-times-circle"></i>'}
              </span>
              <span class="history-command">/${entry.command}${entry.args ? ' ' + entry.args : ''}</span>
            </div>
            <div class="history-item-right">
              <span class="history-time">${this.formatRelativeTime(entry.executed_at)}</span>
              <span class="history-expand-icon"><i class="fa-solid fa-chevron-down"></i></span>
            </div>
          </div>
          <div class="history-item-output hidden">
            <div class="history-output-section">
              <div class="history-output-label">Output:</div>
              <pre class="history-output-content">${this.escapeHtml(entry.stdout || '(no output)')}</pre>
            </div>
            ${entry.stderr ? `
              <div class="history-output-section stderr">
                <div class="history-output-label">Errors:</div>
                <pre class="history-output-content">${this.escapeHtml(entry.stderr)}</pre>
              </div>
            ` : ''}
            <div class="history-meta">
              <span>Exit code: ${entry.return_code}</span>
              <span>User: ${entry.username}</span>
            </div>
          </div>
        </div>
      `).join('');

      // Add click handlers for expand/collapse
      listContainer.querySelectorAll('.history-item-header').forEach(header => {
        header.addEventListener('click', () => {
          const item = header.closest('.command-history-item');
          const output = item.querySelector('.history-item-output');
          const icon = item.querySelector('.history-expand-icon i');

          output.classList.toggle('hidden');
          icon.classList.toggle('fa-chevron-down');
          icon.classList.toggle('fa-chevron-up');
        });
      });

    } catch (error) {
      console.error('Failed to fetch command history:', error);
      listContainer.innerHTML = '<div class="history-error"><i class="fa-solid fa-exclamation-triangle"></i> Failed to load history</div>';
    }
  }

  /**
   * Hide command history modal
   */
  hideHistoryModal() {
    this.historyModal.classList.add('hidden');
  }

  /**
   * Format a timestamp as relative time (e.g., "2 min ago")
   */
  formatRelativeTime(isoString) {
    const date = new Date(isoString);
    const now = new Date();
    const diffMs = now - date;
    const diffSec = Math.floor(diffMs / 1000);
    const diffMin = Math.floor(diffSec / 60);
    const diffHour = Math.floor(diffMin / 60);
    const diffDay = Math.floor(diffHour / 24);

    if (diffSec < 60) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    if (diffHour < 24) return `${diffHour} hour${diffHour > 1 ? 's' : ''} ago`;
    if (diffDay < 7) return `${diffDay} day${diffDay > 1 ? 's' : ''} ago`;
    return date.toLocaleDateString();
  }

  /**
   * Escape HTML to prevent XSS
   */
  escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
  }

  /**
   * Create output modal for viewing single command output
   */
  createOutputModal() {
    this.outputModal = document.createElement('div');
    this.outputModal.className = 'command-output-modal hidden';
    this.outputModal.innerHTML = `
      <div class="command-output-modal-content">
        <div class="modal-header">
          <div class="modal-header-left">
            <span class="output-status-icon"></span>
            <span class="modal-title"></span>
          </div>
          <button class="modal-close"><i class="fa-solid fa-times"></i></button>
        </div>
        <div class="command-output-body">
          <div class="output-section">
            <div class="output-label">Output:</div>
            <pre class="output-content"></pre>
          </div>
          <div class="output-section stderr-section hidden">
            <div class="output-label stderr">Errors:</div>
            <pre class="output-content stderr-content"></pre>
          </div>
          <div class="output-meta"></div>
        </div>
      </div>
    `;

    document.body.appendChild(this.outputModal);

    // Close button handler
    this.outputModal.querySelector('.modal-close').addEventListener('click', () => {
      this.hideOutputModal();
    });

    // Close on backdrop click
    this.outputModal.addEventListener('click', (e) => {
      if (e.target === this.outputModal) {
        this.hideOutputModal();
      }
    });

    // Handle Escape key to close modal
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !this.outputModal.classList.contains('hidden')) {
        this.hideOutputModal();
      }
    });
  }

  /**
   * Show output modal with command result
   */
  showOutputModal(commandData) {
    const { command, args, success, stdout, stderr, return_code } = commandData;

    // Update header
    const statusIcon = this.outputModal.querySelector('.output-status-icon');
    statusIcon.innerHTML = success
      ? '<i class="fa-solid fa-check-circle" style="color: #22c55e;"></i>'
      : '<i class="fa-solid fa-times-circle" style="color: #ef4444;"></i>';

    this.outputModal.querySelector('.modal-title').textContent =
      `/${command}${args ? ' ' + args : ''} - ${success ? 'Success' : 'Failed'}`;

    // Update output content
    this.outputModal.querySelector('.output-content').textContent = stdout || '(no output)';

    // Handle stderr
    const stderrSection = this.outputModal.querySelector('.stderr-section');
    if (stderr) {
      stderrSection.classList.remove('hidden');
      this.outputModal.querySelector('.stderr-content').textContent = stderr;
    } else {
      stderrSection.classList.add('hidden');
    }

    // Update meta
    this.outputModal.querySelector('.output-meta').textContent = `Exit code: ${return_code}`;

    this.outputModal.classList.remove('hidden');
  }

  /**
   * Hide output modal
   */
  hideOutputModal() {
    this.outputModal.classList.add('hidden');
  }

  /**
   * Attach input event listeners
   */
  attachEventListeners() {
    // Listen for input changes to detect "/"
    this.messageInput.addEventListener('input', () => {
      this.handleInput();
    });

    // Keyboard navigation. This has to WIN over ChatApp's Enter-to-send listener,
    // which sits on this same textarea and is registered earlier in boot — two
    // listeners on one element always fire in registration order, so a listener
    // here (or stopPropagation from one) is too late: the message would already
    // have been sent. A capture-phase listener on the document runs before every
    // listener on the input, so the menu gets first refusal on the key.
    const doc = this.messageInput.ownerDocument || document;
    doc.addEventListener('keydown', (e) => this.handleKeydown(e), true);

    // Close dropdown on outside click
    document.addEventListener('click', (e) => {
      if (!this.messageInput.contains(e.target) && !this.dropdown.contains(e.target)) {
        this.hideDropdown();
      }
    });
  }

  /**
   * Menu keys, seen before the composer sees them (capture phase). Anything the
   * menu doesn't claim is left alone so the composer behaves normally.
   */
  handleKeydown(e) {
    if (!this.isOpen) return;
    if (e.target !== this.messageInput) return;

    // Consume the key: the composer must neither send nor type it.
    const claim = () => {
      e.preventDefault();
      e.stopPropagation();
    };

    switch (e.key) {
      case 'ArrowDown':
        claim();
        this.selectNext();
        break;
      case 'ArrowUp':
        claim();
        this.selectPrevious();
        break;
      case 'Enter':
        // Enter picks the highlighted row and leaves the message in the box — a
        // second Enter is the send. Shift+Enter stays a newline, and with nothing
        // highlighted (an empty search) the menu has nothing to give, so it steps
        // aside rather than eating the send key.
        if (e.shiftKey || this.selectedIndex < 0) {
          this.hideDropdown();
          return;
        }
        claim();
        this.executeSelected();
        break;
      case 'Escape':
        claim();
        this.hideDropdown();
        break;
      case 'Tab':
        if (this.selectedIndex >= 0) {
          claim();
          this.autocomplete();
        }
        break;
    }
  }

  /**
   * Handle input changes
   */
  handleInput() {
    const trigger = findSlashTrigger(this.messageInput.value, this.caretPosition());
    if (!trigger) {
      this.hideDropdown();
      return;
    }

    // The cookbook search owns everything after "/cookbook", spaces included.
    if (trigger.kind === 'cookbook') {
      this.showCookbookDropdown(trigger.query, trigger);
      return;
    }

    this.showDropdown(trigger.query, trigger);

    // On menu open (a bare "/") refresh skills so newly authored ones appear
    // without a page reload, then re-render if still open.
    if (trigger.query === '') {
      this.refreshSkills().then(() => {
        const still = findSlashTrigger(this.messageInput.value, this.caretPosition());
        if (still && still.kind === 'command') this.showDropdown(still.query, still);
      });
    }
  }

  /**
   * Fetch available commands from API
   */
  async fetchCommands() {
    await Promise.all([this.fetchHostCommands(), this.refreshSkills()]);
    this.rebuildCommandList();
  }

  /**
   * Fetch privileged host slash commands. This endpoint is engineer/admin gated,
   * so it returns 403 (→ []) for regular users — that's fine, they still get skills.
   */
  async fetchHostCommands() {
    try {
      const response = await fetch('/api/slash-commands');
      this.hostCommands = response.ok ? await response.json() : [];
    } catch (error) {
      console.error('Failed to fetch slash commands:', error);
      this.hostCommands = [];
    }
    this.rebuildCommandList();
  }

  /**
   * Fetch installed Agent Skills and expose each as a slash command. Unlike host
   * commands, picking a skill does NOT run anything on the host — it evokes the
   * skill in the chat (see evokeSkill). Refetched when the menu opens so a newly
   * authored skill shows up without a reload.
   */
  async refreshSkills() {
    try {
      const response = await fetch('/api/skills');
      const skills = response.ok ? await response.json() : [];
      this.skillCommands = skills.map(s => ({
        name: s.slug,
        description: s.description || 'Skill',
        is_skill: true,
        skill_slug: s.slug,
        skill_name: s.name,
      }));
    } catch (error) {
      console.error('Failed to fetch skills:', error);
      this.skillCommands = [];
    }
    this.rebuildCommandList();
  }

  /**
   * Fetch published cookbook recipes through the backend proxy (llamapress.ai
   * serves no CORS headers, so the browser can't read cookbook.json itself).
   * Cached for the session; a failure leaves whatever we already had.
   */
  async fetchCookbook(force = false) {
    const FRESH_MS = 10 * 60 * 1000;
    const fresh = this.cookbookGuides && (Date.now() - this.cookbookFetchedAt) < FRESH_MS;
    if (!force && fresh) return this.cookbookGuides;

    this.cookbookLoading = true;
    try {
      const response = await fetch('/api/cookbook');
      const data = response.ok ? await response.json() : null;
      const guides = Array.isArray(data?.guides) ? data.guides : [];
      // Never replace a good list with an empty one from a failed/stale fetch.
      if (guides.length > 0 || !this.cookbookGuides) {
        this.cookbookGuides = guides;
      }
      this.cookbookFetchedAt = Date.now();
    } catch (error) {
      console.error('Failed to fetch cookbook:', error);
      if (!this.cookbookGuides) this.cookbookGuides = [];
    } finally {
      this.cookbookLoading = false;
    this.slashRange = null;       // the "/…" span a pick writes over
    }
    return this.cookbookGuides;
  }

  /**
   * Render the cookbook search: "/cookbook" lists every recipe, "/cookbook pdf"
   * filters. Renders immediately from cache (or a loading note) and re-renders
   * once the fetch lands, if the user is still searching the cookbook.
   */
  showCookbookDropdown(query = '', range = null) {
    // Where a pick writes back to. Recomputed on every keystroke; kept so a click
    // on a row still knows which "/cookbook …" fragment to swap out.
    this.slashRange = range || findCookbookTrigger(
      this.messageInput.value, this.caretPosition());

    // Recipe rows are two-line prose, and there are ~30 of them — the cookbook
    // list gets its own (much taller) panel height, see .cookbook-mode.
    this.dropdown.classList.add('cookbook-mode');

    if (!this.cookbookGuides) {
      if (!this.cookbookLoading) {
        this.fetchCookbook().then(() => {
          const still = findCookbookTrigger(this.messageInput.value, this.caretPosition());
          if (still) this.showCookbookDropdown(still.query, still);
        });
      }
      this.dropdown.innerHTML =
        '<div class="slash-command-group-header">Cookbook</div>' +
        '<div class="slash-command-empty"><i class="fa-solid fa-spinner fa-spin"></i> Loading recipes…</div>';
      this.dropdown.classList.remove('hidden');
      this.isOpen = true;
      this.selectedIndex = -1;
      this.filteredCommands = [];
      return;
    }

    const filtered = filterCookbookGuides(this.cookbookGuides, query);

    if (filtered.length === 0) {
      const note = this.cookbookGuides.length === 0
        ? "Couldn't load the cookbook — check your connection and try again."
        : `No recipes match "${this._esc(query)}".`;
      this.dropdown.innerHTML =
        '<div class="slash-command-group-header">Cookbook</div>' +
        `<div class="slash-command-empty">${note}</div>`;
      this.dropdown.classList.remove('hidden');
      this.isOpen = true;
      this.selectedIndex = -1;
      this.filteredCommands = [];
      return;
    }

    const count = query
      ? `Cookbook · ${filtered.length} match${filtered.length === 1 ? '' : 'es'}`
      : `Cookbook · ${filtered.length} recipes`;

    this.dropdown.innerHTML =
      `<div class="slash-command-group-header">${count}</div>` +
      filtered.map((g, index) => `
      <div class="slash-command-item cookbook-item${index === 0 ? ' selected' : ''}" data-index="${index}" data-slug="${this._esc(g.slug)}">
        <div class="cookbook-body">
          <span class="cookbook-title">${this._esc(g.title)}${g.personal ? '<span class="command-cookbook-badge cookbook-badge-yours">yours</span>' : ''}${g.category ? `<span class="command-cookbook-badge">${this._esc(g.category)}</span>` : ''}</span>
          <span class="command-description cookbook-summary" title="${this._esc(g.summary)}">${this._esc(g.summary)}</span>
        </div>
        <a class="cookbook-open" href="${this._esc(g.url)}" target="_blank" rel="noopener noreferrer" title="Open this recipe on llamapress.ai"><i class="fa-solid fa-arrow-up-right-from-square"></i></a>
      </div>`).join('');

    this.dropdown.querySelectorAll('.slash-command-item').forEach((item, index) => {
      item.addEventListener('click', (e) => {
        // The ↗ link opens the guide; it must not also fill the composer.
        if (e.target.closest('.cookbook-open')) {
          e.stopPropagation();
          return;
        }
        e.stopPropagation();
        this.selectedIndex = index;
        this.executeSelected();
      });
      item.addEventListener('mouseenter', () => {
        this.setSelectedIndex(index);
      });
    });

    this.dropdown.classList.remove('hidden');
    this.isOpen = true;
    this.selectedIndex = 0;
    this.filteredCommands = filtered.map(g => ({ ...g, is_cookbook: true }));
  }

  /**
   * The "/cookbook" menu entry: switch the input into cookbook search mode and
   * list every recipe. Nothing is sent — the user picks one (or keeps typing to
   * filter) and the pick fills the composer.
   */
  showAllCookbook() {
    // notify:false — we open the recipe list ourselves right below, and an input
    // event would only make handleInput render the same list a second time.
    const caret = this.replaceSlashToken('/cookbook ', { notify: false });
    const start = caret - '/cookbook '.length;
    this.showCookbookDropdown('', { query: '', start, end: caret });
  }

  /** Current caret offset, or the end of the text when the host can't say. */
  caretPosition() {
    const pos = this.messageInput ? this.messageInput.selectionStart : null;
    return typeof pos === 'number' ? pos : undefined;
  }

  /**
   * Picking a recipe swaps the "/cookbook …" the user typed for a short
   * "@cookbook:<slug> (<json url>)" reference — everything else in the composer
   * survives, so you can reach for a recipe mid-thought. It does NOT send, so
   * the user can keep typing "…for my invoices page" before hitting enter.
   */
  insertCookbookMention(guide) {
    this.replaceSlashToken(cookbookMention(guide), { pad: true });
  }

  /**
   * Swap the "/…" the user typed for `text`, leaving the rest of the message
   * exactly as it was, and drop the caret straight after it. Every pick goes
   * through here — that's what lets you reach for a command mid-thought.
   *
   * @param {string} text            what the token becomes ('' removes it)
   * @param {{pad?: boolean, notify?: boolean}} [options]
   *        pad: keep a space between the insert and the neighbouring words.
   *        notify: fire an input event afterwards (re-renders the menu). Off for
   *        picks that open a menu themselves, so they don't render twice.
   */
  replaceSlashToken(text, { pad = false, notify = true } = {}) {
    const range = this.slashRange;
    this.hideDropdown();                 // every pick closes the menu it came from

    const value = String(this.messageInput.value || '');
    const start = range ? Math.min(range.start, value.length) : 0;
    const end = range ? Math.min(Math.max(range.end, start), value.length) : value.length;
    let before = value.slice(0, start);
    let after = value.slice(end);

    let inserted = text;
    if (text === '') {
      // The token is going away entirely — don't leave its spaces behind.
      if (/[ \t]$/.test(before) && /^[ \t]/.test(after)) after = after.replace(/^[ \t]/, '');
    } else if (pad) {
      const lead = before && !/\s$/.test(before) ? ' ' : '';
      const trail = after && !/^\s/.test(after) ? ' ' : (after ? '' : ' ');
      inserted = `${lead}${text}${trail}`;
    }

    this.messageInput.value = before + inserted + after;
    this.messageInput.focus();
    const caret = (before + inserted).length;
    if (this.messageInput.setSelectionRange) {
      this.messageInput.setSelectionRange(caret, caret);
    }
    if (notify) this.messageInput.dispatchEvent(new Event('input', { bubbles: true }));
    return caret;
  }

  /** Merge host commands + the /cookbook entry + the /skills entry + skills. */
  rebuildCommandList() {
    // `/skills` is a meta entry (Claude-style): selecting it — or typing the full
    // word — lists every installed skill. Sits at the top of the Skills section.
    const meta = [{
      name: 'skills',
      description: 'List all available skills',
      is_meta: true,
    }];
    // `/cookbook` searches the published recipes at llamapress.ai/cookbook.
    const cookbook = [{
      name: 'cookbook',
      description: 'Search LlamaPress cookbook recipes',
      is_cookbook_meta: true,
    }];
    this.commands = [
      ...(this.hostCommands || []),
      ...cookbook,
      ...meta,
      ...(this.skillCommands || []),
    ];
  }

  /** Escape user-authored text before injecting into the dropdown HTML. */
  _esc(text) {
    const div = document.createElement('div');
    div.textContent = text == null ? '' : String(text);
    return div.innerHTML;
  }

  /**
   * Show dropdown with filtered commands
   */
  showDropdown(query = '', range = null) {
    this.dropdown.classList.remove('cookbook-mode');
    this.slashRange = range || findSlashTrigger(
      this.messageInput.value, this.caretPosition());

    // Typing "/skills" (or picking the /skills entry) lists EVERY installed skill,
    // even ones whose slug doesn't contain the word "skills".
    const listAllSkills = query === 'skills';
    const filtered = listAllSkills
      ? this.commands.filter(cmd => cmd.is_skill)
      : this.commands.filter(cmd => cmd.name.toLowerCase().includes(query));

    if (filtered.length === 0) {
      if (listAllSkills) {
        // /skills with nothing installed — friendly, non-selectable note.
        this.dropdown.innerHTML =
          '<div class="slash-command-group-header">Skills</div>' +
          '<div class="slash-command-empty">No skills installed yet — ask me to create one.</div>';
        this.dropdown.classList.remove('hidden');
        this.isOpen = true;
        this.selectedIndex = -1;
        this.filteredCommands = [];
        return;
      }
      this.hideDropdown();
      return;
    }

    // Render grouped, with a header before each section. Items stay in the SAME
    // order as `filtered` (commands, then the /skills entry + skills — see
    // rebuildCommandList), so the `.slash-command-item` NodeList index still lines
    // up with filteredCommands for keyboard nav / execute. Headers are not items.
    let html = '';
    let lastGroup = null;
    filtered.forEach((cmd, index) => {
      const group = cmd.is_cookbook_meta ? 'cookbook'
        : (cmd.is_skill || cmd.is_meta) ? 'skills'
        : 'commands';
      if (group !== lastGroup) {
        const label = group === 'cookbook' ? 'Cookbook' : group === 'skills' ? 'Skills' : 'Commands';
        html += `<div class="slash-command-group-header">${label}</div>`;
        lastGroup = group;
      }
      const badge = cmd.is_skill ? '<span class="command-skill-badge"><i class="fa-solid fa-bolt"></i> skill</span>' : '';
      html += `
      <div class="slash-command-item${index === 0 ? ' selected' : ''}${cmd.is_skill ? ' skill-command' : ''}${cmd.is_meta ? ' skills-meta' : ''}${cmd.is_cookbook_meta ? ' cookbook-meta' : ''}" data-command="${this._esc(cmd.name)}" data-index="${index}">
        <span class="command-name">/${this._esc(cmd.name)}${badge}</span>
        <span class="command-description">${this._esc(cmd.description)}</span>
        ${cmd.dangerous ? '<span class="command-warning"><i class="fa-solid fa-exclamation-triangle"></i></span>' : ''}
      </div>`;
    });
    this.dropdown.innerHTML = html;

    // Add click handlers
    this.dropdown.querySelectorAll('.slash-command-item').forEach((item, index) => {
      item.addEventListener('click', (e) => {
        e.stopPropagation();
        this.selectedIndex = index;
        this.executeSelected();
      });
      item.addEventListener('mouseenter', () => {
        this.setSelectedIndex(index);
      });
    });

    this.dropdown.classList.remove('hidden');
    this.isOpen = true;
    this.selectedIndex = 0;
    this.filteredCommands = filtered;
  }

  /**
   * Hide dropdown
   */
  hideDropdown() {
    this.dropdown.classList.add('hidden');
    this.dropdown.classList.remove('cookbook-mode');
    this.slashRange = null;
    this.isOpen = false;
    this.selectedIndex = -1;
    this.filteredCommands = [];
  }

  /**
   * Select next item in dropdown
   */
  selectNext() {
    const items = this.dropdown.querySelectorAll('.slash-command-item');
    if (items.length === 0) return;

    const newIndex = Math.min(this.selectedIndex + 1, items.length - 1);
    this.setSelectedIndex(newIndex);
  }

  /**
   * Select previous item in dropdown
   */
  selectPrevious() {
    const newIndex = Math.max(this.selectedIndex - 1, 0);
    this.setSelectedIndex(newIndex);
  }

  /**
   * Set selected index and update UI
   */
  setSelectedIndex(index) {
    const items = this.dropdown.querySelectorAll('.slash-command-item');
    items.forEach((item, i) => {
      item.classList.toggle('selected', i === index);
    });
    this.selectedIndex = index;

    // Scroll item into view if needed
    const selectedItem = items[index];
    if (selectedItem) {
      selectedItem.scrollIntoView({ block: 'nearest' });
    }
  }

  /**
   * Autocomplete the command in the input
   */
  autocomplete() {
    if (this.selectedIndex < 0 || !this.filteredCommands) return;

    const cmd = this.filteredCommands[this.selectedIndex];
    if (!cmd) return;

    // Cookbook recipes have no slash token to complete — Tab drops the recipe
    // reference into the composer, same as Enter.
    if (cmd.is_cookbook) {
      this.insertCookbookMention(cmd);
      return;
    }

    this.messageInput.value = `/${cmd.name}`;
    this.hideDropdown();
  }

  /**
   * Execute selected command (shows confirmation first, or history modal for /history)
   */
  executeSelected() {
    if (this.selectedIndex < 0 || !this.filteredCommands) return;

    const cmd = this.filteredCommands[this.selectedIndex];

    if (!cmd) {
      this.hideDropdown();
      return;
    }

    // A picked cookbook recipe fills the composer; nothing runs on the host.
    if (cmd.is_cookbook) {
      this.insertCookbookMention(cmd);
      return;
    }

    // The /cookbook entry switches into cookbook search instead of executing.
    if (cmd.is_cookbook_meta) {
      this.showAllCookbook();
      return;
    }

    // The /skills entry lists every skill instead of executing anything.
    if (cmd.is_meta && cmd.name === 'skills') {
      this.showAllSkills();
      return;
    }

    // Skills do NOT execute like host commands — they evoke the skill in the chat.
    // Keep the input (evokeSkill fills it with a directive) rather than clearing it.
    if (cmd.is_skill) {
      this.evokeSkill(cmd);
      return;
    }

    // Handle /history specially - show history modal directly
    if (cmd.name === 'history') {
      this.showHistoryModal();
    } else if (cmd.name === 'gh') {
      this.openGitHubAuthModal();
    } else {
      this.showConfirmModal(cmd);
    }

    // The command isn't part of the message — drop its token and keep the rest of
    // what the user was writing.
    this.replaceSlashToken('', { notify: false });
  }

  /**
   * Evoke a skill from the slash menu. Does NOT send anything — it drops the
   * skill's slash token "/<slug> " into the input and focuses it, so the user
   * can add their own context before sending. The agent recognizes a leading
   * "/<slug>" (that matches an installed skill) as a request to call use_skill
   * for that slug — see the "User-invoked skills" section of the agent prompt.
   */
  evokeSkill(cmd) {
    const slug = cmd.skill_slug || cmd.name;
    // Only the "/…" the user typed is replaced, so a skill can be picked in the
    // middle of a sentence. The trailing space closes the menu (the token is done)
    // and lets the user type their request right after it.
    this.replaceSlashToken(`/${slug}`, { pad: true });
  }

  /**
   * Show the full skills list (the /skills command). Sets the input to "/skills"
   * and re-renders the dropdown listing every installed skill; refetches first so
   * the list is current. Picking one from the list then evokes it.
   */
  showAllSkills() {
    const caret = this.replaceSlashToken('/skills', { notify: false });
    const range = { query: 'skills', start: caret - '/skills'.length, end: caret };
    this.showDropdown('skills', range);                  // immediate (cached)
    this.refreshSkills().then(() => {                    // then refresh + re-render
      const still = findSlashTrigger(this.messageInput.value, this.caretPosition());
      if (still && still.kind === 'command' && still.query === 'skills') {
        this.showDropdown('skills', still);
      }
    });
  }

  /**
   * Show confirmation modal
   */
  showConfirmModal(command) {
    this.confirmModal.querySelector('.modal-command').textContent = `/${command.name}`;
    this.confirmModal.querySelector('.modal-message').textContent = command.confirm_message;

    const confirmBtn = this.confirmModal.querySelector('.modal-confirm');
    confirmBtn.className = `modal-confirm ${command.dangerous ? 'dangerous' : ''}`;

    // Remove old listener and add new one
    const newConfirmBtn = confirmBtn.cloneNode(true);
    confirmBtn.parentNode.replaceChild(newConfirmBtn, confirmBtn);

    newConfirmBtn.addEventListener('click', () => {
      this.executeCommand(command.name);
      this.hideConfirmModal();
    });

    this.confirmModal.classList.remove('hidden');
  }

  /**
   * Hide confirmation modal
   */
  hideConfirmModal() {
    this.confirmModal.classList.add('hidden');
  }

  /**
   * Execute command via API
   */
  async executeCommand(commandName) {
    try {
      // Show executing message in chat
      this.showSystemMessage(`Executing /${commandName}...`, 'info');

      const response = await fetch('/api/slash-commands/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ command: commandName })
      });

      // Handle restart command specially - the server may restart before responding
      if (commandName === 'restart') {
        // Try to parse response, but if it fails, assume restart was initiated
        try {
          const text = await response.text();
          if (text) {
            const result = JSON.parse(text);
            if (result.success) {
              this.showSystemMessage(`/${commandName} initiated. The page will reload shortly...`, 'success', result);
            } else {
              this.showSystemMessage(`/${commandName} failed:\n\n${result.output}`, 'error', result);
            }
          } else {
            // Empty response - server likely restarted
            this.showSystemMessage(`/${commandName} initiated. The server is restarting...`, 'info');
          }
        } catch (parseError) {
          // JSON parse failed - server likely restarted mid-response
          this.showSystemMessage(`/${commandName} initiated. The server is restarting...`, 'info');
        }
        return;
      }

      const result = await response.json();

      // Handle special commands with custom frontend behavior
      if (result.special_handler === 'gh_auth_modal') {
        this.openGitHubAuthModal();
        return;
      }

      if (result.special_handler === 'compact') {
        this.executeCompact();
        return;
      }

      if (result.success) {
        this.showSystemMessage(`/${commandName} completed successfully:\n\n${result.output}`, 'success', result);
      } else {
        this.showSystemMessage(`/${commandName} failed:\n\n${result.output}`, 'error', result);
      }

    } catch (error) {
      // For restart command, network errors are expected
      if (commandName === 'restart') {
        this.showSystemMessage(`/${commandName} initiated. The server is restarting...`, 'info');
        return;
      }
      console.error('Failed to execute command:', error);
      this.showSystemMessage(`Error executing /${commandName}: ${error.message}`, 'error');
    }
  }

  /**
   * Execute /compact by routing it through the normal WebSocket pipeline.
   * The backend intercepts the "/compact" message before it reaches the agent,
   * streams a thinking shimmer + the generated summary back as AIMessageChunk
   * events, updates the checkpoint, and sends a token_usage update so the
   * context wheel refreshes immediately — identical to the auto-summarization UX.
   */
  executeCompact() {
    if (!this.chatApp) return;

    const threadId = this.chatApp?.appState?.currentThreadId;
    if (!threadId) {
      this.showSystemMessage('No active conversation to compact — start chatting first.', 'error');
      return;
    }

    // Populate the input and call sendMessage() so the full streaming pipeline fires:
    // user message shows in chat → WS sends → backend intercepts → thinking + streaming summary → token wheel update
    this.messageInput.value = '/compact';
    this.chatApp.sendMessage();
  }

  /**
   * Open the GitHub auth modal (used by /gh command and checkpoint panel button)
   */
  openGitHubAuthModal() {
    // Use the shared GitHubAuthModal - import lazily to avoid circular deps
    import('../checkpoints/GitHubAuthModal.js').then(({ GitHubAuthModal }) => {
      const modal = new GitHubAuthModal();
      modal.start();
    });
  }

  /**
   * Show a system message in the chat
   */
  showSystemMessage(message, type = 'info', commandData = null) {
    // Create and dispatch a custom event that ChatApp can listen to
    const event = new CustomEvent('slashCommandMessage', {
      detail: { message, type }
    });
    window.dispatchEvent(event);

    // Also show as a notification toast
    this.showToast(message, type, commandData);
  }

  /**
   * Show a toast notification
   */
  showToast(message, type = 'info', commandData = null) {
    // Remove existing toasts
    const existingToasts = document.querySelectorAll('.slash-command-toast');
    existingToasts.forEach(t => t.remove());

    const toast = document.createElement('div');
    toast.className = `slash-command-toast ${type}`;

    // Make toast clickable if we have command data
    if (commandData) {
      toast.classList.add('clickable');
    }

    // Truncate long messages for the toast
    const truncatedMessage = message.length > 200
      ? message.substring(0, 200) + '...'
      : message;

    const clickHint = commandData ? '<div class="toast-hint">Click to view full output</div>' : '';

    toast.innerHTML = `
      <div class="toast-icon">
        ${type === 'success' ? '<i class="fa-solid fa-check-circle"></i>' : ''}
        ${type === 'error' ? '<i class="fa-solid fa-times-circle"></i>' : ''}
        ${type === 'info' ? '<i class="fa-solid fa-info-circle"></i>' : ''}
      </div>
      <div class="toast-content">
        <div class="toast-message">${truncatedMessage.replace(/\n/g, '<br>')}</div>
        ${clickHint}
      </div>
      <button class="toast-close"><i class="fa-solid fa-times"></i></button>
    `;

    document.body.appendChild(toast);

    // Make toast body clickable to show output modal
    if (commandData) {
      const toastContent = toast.querySelector('.toast-content');
      toastContent.addEventListener('click', (e) => {
        e.stopPropagation();
        this.showOutputModal(commandData);
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 300);
      });
    }

    // Close button handler
    toast.querySelector('.toast-close').addEventListener('click', (e) => {
      e.stopPropagation();
      toast.classList.add('hiding');
      setTimeout(() => toast.remove(), 300);
    });

    // Auto-remove after delay (longer for errors)
    const delay = type === 'error' ? 10000 : 5000;
    setTimeout(() => {
      if (toast.parentElement) {
        toast.classList.add('hiding');
        setTimeout(() => toast.remove(), 300);
      }
    }, delay);

    // Trigger animation
    requestAnimationFrame(() => {
      toast.classList.add('visible');
    });
  }
}
