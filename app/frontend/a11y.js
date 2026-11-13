/**
 * Keyboard and screen-reader layer shared by every LlamaBot page (HECVAT ITAC-17, LP-34).
 *
 * The UI builds most of its controls at runtime and has about twenty modals across
 * many files, so this handles them in one place instead of patching each:
 *   - clickable <div>s become focusable and answer Enter/Space
 *   - modals trap Tab, close on Escape, and hand focus back to whatever opened them
 *   - menus and panels report aria-expanded and close on Escape
 *   - status changes reach a polite live region
 * Classic script, not a module, so it also runs on the pages rendered from ui.py.
 */
(function () {
  const llamabot = (name) => `[data-llamabot="${name}"]`;

  // Clickable elements that are not native controls. Plain [onclick] covers the
  // pages rendered from ui.py.
  const CLICK_TARGETS = [
    '[onclick]',
    '.execution-mode-option',
    '.tab-text',
    '.menu-items .menu-item',
    '.thinking-message-header',
    '[data-llamabot="tool-compact"]',
    '.git-graph-node',
    '.git-commit-item',
    '.git-commit-sha-badge',
    '.checkpoint-detail-sha',
    '.history-item-header',
    '.prompt-item',
    '.prompt-panel-tab',
    '.skill-item',
    '.asset-row',
    '.asset-sheet-tab',
    '.plan-question-dot',
  ].join(',');

  const NATIVE = 'a[href],button,input,select,textarea,summary';
  const FOCUSABLE = `${NATIVE},[tabindex]:not([tabindex="-1"])`;

  const MODALS = [
    '.modal',
    '[class$="-modal"]',
    '[class*="-modal "]',
    '.update-modal-overlay',
    '.js-error-modal-overlay',
  ].join(',');

  const CLOSE_CONTROLS = [
    '[data-llamabot$="-close"]',
    '[data-llamabot$="-cancel"]',
    '[data-llamabot$="-minimize"]',
    '[data-llamabot$="-dismiss-error"]',
    '[aria-label="Close"]',
    '[class*="close"]',
    '[class*="-cancel"]',
    '[class$="-x"]',
  ].join(',');

  // close: what Escape presses. moveFocus: the panel is not next to its trigger in
  // tab order, so opening it has to take focus there.
  const POPOVERS = [
    { trigger: 'hamburger-menu', panel: 'menu-drawer', isOpen: (el) => el.classList.contains('open'), moveFocus: true },
    { trigger: 'execution-mode-trigger', panel: 'execution-mode-menu', moveFocus: true, menu: true },
    { trigger: 'file-attach-btn', panel: 'file-attach-menu', moveFocus: true, menu: true },
    { trigger: 'browse-files-btn', panel: 'file-browser-panel', close: 'file-browser-close', moveFocus: true },
    { trigger: 'tools-toggle-btn', panel: 'tools-toolbar' },
    { trigger: 'checkpoint-btn', panel: 'checkpoint-panel', closeClass: '.close-checkpoint-panel', moveFocus: true },
  ];

  // Announced when they appear.
  const BANNERS = [
    'timeout-warning',
    'update-banner',
    'image-switch-banner',
    'vision-disabled-banner',
    'model-switch-banner',
    'js-error-banner',
  ];

  const VERB_ANNOUNCE_GAP_MS = 15000;

  const isVisible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const focusables = (root) => [...root.querySelectorAll(FOCUSABLE)].filter((el) => isVisible(el) && !el.disabled);

  function announcer() {
    let el = document.querySelector(llamabot('status-announcer'));
    if (!el) {
      el = document.createElement('div');
      el.className = 'sr-only';
      el.dataset.llamabot = 'status-announcer';
      el.setAttribute('role', 'status');
      el.setAttribute('aria-live', 'polite');
      el.setAttribute('aria-atomic', 'true');
      document.body.appendChild(el);
    }
    return el;
  }

  let lastAnnounceAt = 0;
  function announce(text) {
    lastAnnounceAt = Date.now();
    announcer().textContent = text;
  }

  // --- Clickable elements -------------------------------------------------------

  function upgradeClickTargets() {
    document.querySelectorAll(CLICK_TARGETS).forEach((el) => {
      if (el.matches(NATIVE) || el.hasAttribute('tabindex')) return;
      if (el.matches('.menu-item') && !el.onclick && !el.hasAttribute('onclick')) return;
      el.setAttribute('tabindex', '0');
      // A button may not contain another control, so a row with its own actions
      // stays focusable without claiming the role.
      if (!el.getAttribute('role') && !el.querySelector(`${FOCUSABLE},${CLICK_TARGETS}`)) el.setAttribute('role', 'button');
    });
  }

  function syncNames() {
    document.querySelectorAll('.tab .tab-text').forEach((text) => {
      const active = text.closest('.tab').classList.contains('active');
      if (active) text.setAttribute('aria-current', 'page');
      else text.removeAttribute('aria-current');
    });
    document.querySelectorAll('.execution-mode-option').forEach((option) => {
      option.setAttribute('role', 'menuitemradio');
      option.setAttribute('aria-checked', String(option.querySelector('.execution-mode-check') !== null));
    });
    const message = document.getElementById('message');
    const form = message && document.querySelector('form');
    if (form) {
      form.querySelectorAll('input').forEach((input) => {
        input.setAttribute('aria-invalid', String(message.classList.contains('error')));
      });
    }
  }

  // --- Menus and panels ---------------------------------------------------------

  const popoverOpen = (popover, panel) => (popover.isOpen ? popover.isOpen(panel) : isVisible(panel));

  function popoverElements(popover) {
    const panel = document.querySelector(llamabot(popover.panel));
    const trigger = document.querySelector(llamabot(popover.trigger));
    return panel && trigger ? { panel, trigger } : null;
  }

  function closePopover(popover, { panel, trigger }) {
    const closer =
      (popover.close && panel.querySelector(llamabot(popover.close))) ||
      (popover.closeClass && panel.querySelector(popover.closeClass)) ||
      trigger;
    closer.click();
  }

  function syncPopovers() {
    POPOVERS.forEach((popover) => {
      const els = popoverElements(popover);
      if (!els) return;
      const { panel, trigger } = els;
      const open = popoverOpen(popover, panel);
      if (popover.menu) {
        panel.setAttribute('role', 'menu');
        panel.querySelectorAll('button').forEach((item) => item.setAttribute('role', 'menuitem'));
      }
      trigger.setAttribute('aria-expanded', String(open));

      if (open && !popover.wasOpen && popover.moveFocus && document.activeElement === trigger) {
        focusables(panel)[0]?.focus();
      }
      if (!open && popover.wasOpen) {
        const active = document.activeElement;
        if (!active || active === document.body || panel.contains(active)) trigger.focus();
      }
      popover.wasOpen = open;
    });
  }

  // --- Modals -------------------------------------------------------------------

  let openModal = null;
  let modalOpener = null;

  function currentModal() {
    const visible = [...document.querySelectorAll(MODALS)].filter(isVisible);
    return visible.filter((el) => !visible.some((other) => other !== el && other.contains(el))).pop() || null;
  }

  function syncModal() {
    const modal = currentModal();
    if (modal === openModal) return;

    if (modal) {
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      const heading = modal.querySelector('h1,h2,h3,[class*="title"]');
      if (heading && !modal.hasAttribute('aria-label')) {
        modal.setAttribute('aria-label', heading.textContent.trim().slice(0, 80));
      }
      if (!openModal) modalOpener = document.activeElement;
      if (!modal.contains(document.activeElement)) {
        const first = focusables(modal)[0];
        if (first) first.focus();
        else {
          modal.setAttribute('tabindex', '-1');
          modal.focus();
        }
      }
    } else if (modalOpener) {
      // The opener may have closed along with its menu; the composer is the fallback.
      const opener = modalOpener !== document.body && isVisible(modalOpener) ? modalOpener : null;
      const target = opener || document.querySelector(llamabot('message-input'));
      target?.focus();
      modalOpener = null;
    }
    openModal = modal;
  }

  function closeControl(modal) {
    const named = [...modal.querySelectorAll(CLOSE_CONTROLS)].find(isVisible);
    if (named) return named;
    return [...modal.querySelectorAll('button')].find(
      (button) => isVisible(button) && /^(cancel|close|not now|dismiss|×)$/i.test(button.textContent.trim())
    );
  }

  function trapTab(event, modal) {
    const items = focusables(modal);
    if (!items.length) {
      event.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const inside = modal.contains(document.activeElement);
    if (event.shiftKey && (document.activeElement === first || !inside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || !inside)) {
      event.preventDefault();
      first.focus();
    }
  }

  // --- Agent status -------------------------------------------------------------

  let lastVerb = '';
  let wasConnected = null;
  const bannerWasVisible = {};

  function syncStatus() {
    const verbEl = document.querySelector(`${llamabot('thinking-area')} .typing-indicator`);
    const verb = (verbEl?.textContent || '').replace(/[^\p{L}\p{N} .,'-]/gu, '').trim();
    if (verb && verb !== lastVerb && Date.now() - lastAnnounceAt >= VERB_ANNOUNCE_GAP_MS) {
      announce(verb);
    }
    lastVerb = verb;

    const status = document.querySelector(llamabot('connection-status'));
    if (status) {
      const connected = status.classList.contains('connected');
      if (wasConnected !== null && connected !== wasConnected) {
        announce(connected ? 'Connected.' : 'Connection lost. Trying to reconnect.');
      }
      wasConnected = connected;
    }

    BANNERS.forEach((name) => {
      const banner = document.querySelector(llamabot(name));
      if (!banner) return;
      const visible = isVisible(banner);
      if (visible && !bannerWasVisible[name] && banner.textContent.trim()) {
        announce(banner.textContent.replace(/\s+/g, ' ').trim().slice(0, 300));
      }
      bannerWasVisible[name] = visible;
    });
  }

  /** Called by the chat when a turn starts or ends. */
  function agentRunningChanged(running) {
    if (running) {
      announce('Leo is working.');
      return;
    }
    const replies = document.querySelectorAll(llamabot('ai-message'));
    const reply = replies[replies.length - 1]?.textContent.replace(/\s+/g, ' ').trim().slice(0, 300);
    announce(reply ? `Leo finished responding: ${reply}` : 'Leo finished responding.');
  }

  // --- Wiring -------------------------------------------------------------------

  function sync() {
    upgradeClickTargets();
    syncNames();
    syncModal();
    syncPopovers();
    syncStatus();
  }

  let pending = null;
  const scheduleSync = () => {
    if (pending) return;
    pending = setTimeout(() => {
      pending = null;
      sync();
    }, 50);
  };

  function moveInMenu(event) {
    const menu = document.activeElement?.closest?.('[role="menu"]');
    if (!menu || !['ArrowDown', 'ArrowUp'].includes(event.key)) return;
    event.preventDefault();
    const items = focusables(menu);
    const step = event.key === 'ArrowDown' ? 1 : -1;
    items[(items.indexOf(document.activeElement) + step + items.length) % items.length]?.focus();
  }

  document.addEventListener('keydown', (event) => {
    const target = event.target;
    if ((event.key === 'Enter' || event.key === ' ') && target.matches?.(CLICK_TARGETS) && !target.matches(NATIVE)) {
      event.preventDefault();
      target.click();
      scheduleSync();
      return;
    }
    moveInMenu(event);
  });

  // On window, so a modal's own handler runs first and a lock modal's blocker wins.
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Tab') {
      const modal = currentModal();
      if (modal) trapTab(event, modal);
      return;
    }
    if (event.key !== 'Escape' || event.defaultPrevented) return;

    const modal = currentModal();
    if (modal) {
      closeControl(modal)?.click();
      scheduleSync();
      return;
    }
    const popover = POPOVERS.find((p) => {
      const els = popoverElements(p);
      return els && popoverOpen(p, els.panel);
    });
    if (popover) {
      const els = popoverElements(popover);
      closePopover(popover, els);
      els.trigger.focus();
      scheduleSync();
    }
  });

  // Leaving a menu with Tab closes it, as it would for a native one.
  document.addEventListener('focusin', (event) => {
    POPOVERS.filter((p) => p.menu).forEach((popover) => {
      const els = popoverElements(popover);
      if (!els || !popoverOpen(popover, els.panel)) return;
      if (!els.panel.contains(event.target) && event.target !== els.trigger) {
        closePopover(popover, els);
      }
    });
  });

  function start() {
    announcer();
    sync();
    new MutationObserver(scheduleSync).observe(document.body, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ['class', 'style', 'hidden'],
    });
  }

  window.LeoA11y = { announce, agentRunningChanged };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
