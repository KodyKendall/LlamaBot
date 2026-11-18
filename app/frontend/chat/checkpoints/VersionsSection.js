/**
 * Versions section at the top of the History panel (LP-47).
 *
 * Save, restore, delete and "start new Leo from this" for whole-app versions
 * (code + database + uploaded files). Versions live on LlamaPress; nothing here
 * ever downloads one. Server text is always set with textContent.
 *
 * The confirm dialog uses the "-modal" class so a11y.js gives it the dialog
 * role, the focus trap, Escape-to-cancel and focus return on close.
 */

import {
  describeVersions, errorView, restoreConfirmText, launchConfirmText, restoringText,
  launchedText, confirmMatches, restoreFinished, makeVersionsApi,
  RESTORE_SLOW_TEXT, RESTORE_TIMEOUT_MS,
} from './versionsView.js';

const SAVING_POLL_MS = 5000;
const RESTORE_POLL_MS = 4000;

function el(tag, attrs = {}, text = '') {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') node.className = value;
    else node.setAttribute(key, value);
  }
  if (text) node.textContent = text;
  return node;
}

export class VersionsSection {
  constructor(mount, { api = makeVersionsApi(), reload = () => window.location.reload() } = {}) {
    this.mount = mount;
    this.api = api;
    this.reload = reload;
    this.view = null;
    this.savingTimer = null;
    this.restoring = false;
    this.savingIds = new Set();
    this.build();
  }

  build() {
    const section = el('section', {
      class: 'versions-section hidden', 'data-llamabot': 'versions-section', 'aria-labelledby': 'versions-heading',
    });
    const header = el('div', { class: 'versions-header' });
    header.append(el('h4', { id: 'versions-heading' }, 'Versions'));
    this.saveButton = el('button', { type: 'button', class: 'btn-save-version' }, 'Save version');
    this.saveButton.onclick = () => this.showSaveForm();
    header.append(this.saveButton);

    const help = el('p', { class: 'versions-help' },
      'Saves your whole app — code, database and uploaded files — so you can go back to it.');

    this.form = el('form', { class: 'save-version-form hidden' });
    const label = el('label', { for: 'version-name-input' }, 'Version name');
    this.nameInput = el('input', {
      type: 'text', id: 'version-name-input', maxlength: '80', placeholder: 'e.g. before invoices', autocomplete: 'off',
    });
    const actions = el('div', { class: 'save-version-actions' });
    const submit = el('button', { type: 'submit', class: 'btn-confirm-save-version' }, 'Save');
    const cancel = el('button', { type: 'button', class: 'btn-cancel-save-version' }, 'Cancel');
    cancel.onclick = () => this.hideSaveForm();
    actions.append(submit, cancel);
    this.form.append(label, this.nameInput, actions);
    this.form.onsubmit = (event) => {
      event.preventDefault();
      this.save();
    };
    this.form.onkeydown = (event) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        this.hideSaveForm();
      }
    };

    // Saving / Saved / errors, announced as text — never colour alone.
    this.status = el('div', { class: 'versions-status', role: 'status', 'aria-live': 'polite' });
    this.statusText = el('span', { class: 'versions-status-text' });
    this.statusLink = el('a', {
      class: 'versions-upgrade-link hidden', target: '_blank', rel: 'noopener',
    }, 'See plans and pricing');
    this.status.append(this.statusText, this.statusLink);

    this.restoringBox = el('div', { class: 'versions-restoring hidden', role: 'status', 'aria-live': 'assertive' });

    this.list = el('ul', { class: 'versions-list', 'aria-label': 'Saved versions' });
    this.empty = el('p', { class: 'versions-empty hidden' }, 'No saved versions yet.');

    section.append(header, help, this.form, this.status, this.restoringBox, this.list, this.empty);
    this.section = section;
    this.mount?.append(section);
  }

  // ------------------------------------------------------------------ state

  setStatus(text, linkUrl = '', linkText = 'See plans and pricing') {
    this.statusText.textContent = text || '';
    if (linkUrl) {
      this.statusLink.textContent = linkText;
      this.statusLink.href = linkUrl;
      this.statusLink.classList.remove('hidden');
    } else {
      this.statusLink.removeAttribute('href');
      this.statusLink.classList.add('hidden');
    }
  }

  showError(body) {
    const { message, upgradeUrl } = errorView(body);
    this.setStatus(message, upgradeUrl);
  }

  async refresh() {
    if (this.restoring) return;
    let result;
    try {
      result = await this.api.list();
    } catch (e) {
      this.section.classList.remove('hidden');
      this.setStatus("Couldn't load saved versions. Please try again in a moment.");
      return;
    }
    if (!result.ok) {
      this.section.classList.remove('hidden');
      this.showError(result.body);
      return;
    }
    this.render(describeVersions(result.body));
    if (this.view.restoring) this.followRestore(this.view.items.find((v) => v.id === this.view.restoringVersionId));
  }

  render(view) {
    this.view = view;
    this.section.classList.toggle('hidden', view.hidden);
    if (view.hidden) return;

    this.saveButton.classList.toggle('hidden', !view.canSave);
    if (!view.enabled) this.setStatus(view.message, view.upgradeUrl);
    this.announceFinishedSaves(view.items);

    this.list.replaceChildren(...view.items.map((item) => this.renderItem(item, view)));
    this.empty.classList.toggle('hidden', !view.empty);

    clearTimeout(this.savingTimer);
    if (view.anySaving) this.savingTimer = setTimeout(() => this.refresh(), SAVING_POLL_MS);
  }

  announceFinishedSaves(items) {
    for (const item of items) {
      if (item.status === 'saving') this.savingIds.add(item.id);
      else if (this.savingIds.delete(item.id) && item.ready) this.setStatus(`"${item.name}" saved.`);
      else if (item.status === 'failed' && this.savingIds.delete(item.id)) this.setStatus(`"${item.name}" couldn't be saved.`);
    }
  }

  renderItem(item, view) {
    const li = el('li', { class: 'version-item', 'data-version-id': item.id });
    const top = el('div', { class: 'version-item-top' });
    top.append(el('span', { class: 'version-name' }, item.name));
    const badge = el('span', { class: `version-badge version-badge-${item.status}` }, item.statusLabel);
    top.append(badge);
    li.append(top);

    const meta = [item.date, item.size].filter(Boolean).join(' · ');
    if (meta) li.append(el('div', { class: 'version-meta' }, meta));
    if (item.safety) li.append(el('div', { class: 'version-meta' }, 'Saved automatically before a restore'));

    const actions = el('div', { class: 'version-actions' });
    if (view.canRestore) {
      const restore = el('button', {
        type: 'button', class: 'btn-version-restore', 'aria-label': `Restore version ${item.name}`,
      }, 'Restore');
      restore.disabled = !item.ready;
      restore.onclick = () => this.confirmRestore(item, restore);
      actions.append(restore);
    }
    if (view.canLaunch) {
      const launch = el('button', {
        type: 'button', class: 'btn-version-launch', 'aria-label': `Start new Leo from this: ${item.name}`,
      }, 'Start new Leo from this');
      launch.disabled = !item.ready;
      launch.onclick = () => this.confirmLaunch(item, launch);
      actions.append(launch);
    }
    const remove = el('button', {
      type: 'button', class: 'btn-version-delete', 'aria-label': `Delete version ${item.name}`,
    }, 'Delete');
    remove.onclick = () => this.confirmDelete(item, remove);
    actions.append(remove);
    li.append(actions);
    return li;
  }

  // ------------------------------------------------------------------ save

  showSaveForm() {
    this.form.classList.remove('hidden');
    this.nameInput.value = '';
    this.nameInput.focus();
  }

  hideSaveForm() {
    this.form.classList.add('hidden');
    this.saveButton.focus();
  }

  async save() {
    const name = this.nameInput.value.trim();
    if (!name) {
      this.setStatus('Give this version a name.');
      this.nameInput.focus();
      return;
    }
    this.setStatus(`Saving "${name}"…`);
    let result;
    try {
      result = await this.api.save(name);
    } catch (e) {
      this.setStatus("Couldn't reach LlamaPress. Please try again in a moment.");
      return;
    }
    if (!result.ok) {
      this.showError(result.body);
      return;
    }
    this.form.classList.add('hidden');
    this.saveButton.focus();
    if (result.body.version?.id) this.savingIds.add(result.body.version.id);
    await this.refresh();
  }

  // ------------------------------------------------------------------ confirm dialog

  /**
   * Plain-language confirmation. With typedName, the confirm button stays off
   * until the user types the version's name. Resolves true/false.
   */
  confirm({ title, text, confirmLabel, typedName = null, opener = null }) {
    return new Promise((resolve) => {
      const overlay = el('div', {
        class: 'version-confirm-modal', role: 'dialog', 'aria-modal': 'true',
        'aria-labelledby': 'version-confirm-title', 'aria-describedby': 'version-confirm-text',
        'data-llamabot': 'version-confirm-modal',
      });
      const card = el('div', { class: 'version-confirm-card' });
      card.append(el('h3', { id: 'version-confirm-title' }, title));
      card.append(el('p', { id: 'version-confirm-text' }, text));

      let input = null;
      if (typedName) {
        const label = el('label', { for: 'version-confirm-input' });
        label.append('Type ', el('strong', {}, typedName), ' to confirm');
        input = el('input', { type: 'text', id: 'version-confirm-input', autocomplete: 'off' });
        card.append(label, input);
      }

      const actions = el('div', { class: 'version-confirm-actions' });
      const ok = el('button', { type: 'button', class: 'btn-version-confirm' }, confirmLabel);
      const cancel = el('button', { type: 'button', class: 'btn-version-cancel' }, 'Cancel');
      // Cancel first: it is where focus lands on a dialog with nothing to type.
      actions.append(cancel, ok);
      card.append(actions);
      overlay.append(card);

      const sync = () => { ok.disabled = typedName ? !confirmMatches(input.value, typedName) : false; };
      sync();
      input?.addEventListener('input', sync);

      const close = (answer) => {
        overlay.remove();
        document.removeEventListener('keydown', onKey, true);
        (opener && opener.isConnected ? opener : this.saveButton)?.focus();
        resolve(answer);
      };
      const onKey = (event) => {
        if (event.key === 'Escape') {
          event.preventDefault();
          event.stopPropagation();
          close(false);
        } else if (event.key === 'Enter' && event.target === input && !ok.disabled) {
          event.preventDefault();
          close(true);
        } else if (event.key === 'Tab') {
          // Keep focus inside the dialog.
          const items = [...overlay.querySelectorAll('input,button')].filter((n) => !n.disabled);
          const first = items[0];
          const last = items[items.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }
      };
      ok.onclick = () => close(true);
      cancel.onclick = () => close(false);
      document.addEventListener('keydown', onKey, true);
      // Focus moves in on a11y.js's next sync (it records the opener first, and
      // returns focus there on close). Moving it here would make the dialog its
      // own opener, and focus would fall back to the composer on close.
      document.body.append(overlay);
      setTimeout(() => {
        if (overlay.isConnected && !overlay.contains(document.activeElement)) (input || cancel).focus();
      }, 120);
    });
  }

  // ------------------------------------------------------------------ restore

  async confirmRestore(item, opener) {
    const yes = await this.confirm({
      title: `Restore "${item.name}"?`,
      text: restoreConfirmText(item),
      confirmLabel: 'Restore my app',
      typedName: item.name,
      opener,
    });
    if (!yes) return;
    this.setStatus('Saving your current app first…');
    let result;
    try {
      result = await this.api.restore(item.id, item.name);
    } catch (e) {
      // The box may already be going down for the restore.
      result = { ok: true, body: {} };
    }
    if (!result.ok) {
      this.showError(result.body);
      return;
    }
    this.followRestore(item);
  }

  async followRestore(item) {
    if (this.restoring) return;
    this.restoring = true;
    clearTimeout(this.savingTimer);
    this.setStatus('');
    this.restoringBox.textContent = restoringText(item?.name || 'your saved version');
    this.restoringBox.classList.remove('hidden');
    this.list.classList.add('hidden');
    this.saveButton.disabled = true;

    const deadline = Date.now() + RESTORE_TIMEOUT_MS;
    let sawOutage = false;
    while (Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, RESTORE_POLL_MS));
      let poll;
      try {
        const result = await this.api.list();
        poll = { ok: result.ok, body: result.body, sawOutage };
        if (!result.ok && result.status >= 500) sawOutage = true;
      } catch (e) {
        sawOutage = true;
        continue;
      }
      if (restoreFinished(poll)) {
        // Reloading reconnects the chat and the preview to the restored app.
        this.restoringBox.textContent = 'Your app is back. Reconnecting…';
        this.reload();
        return;
      }
    }
    this.restoringBox.textContent = RESTORE_SLOW_TEXT;
  }

  // ------------------------------------------------------------------ start new Leo

  async confirmLaunch(item, opener) {
    const yes = await this.confirm({
      title: `Start a new Leo from "${item.name}"?`,
      text: launchConfirmText(item),
      confirmLabel: 'Start new Leo',
      opener,
    });
    if (!yes) return;
    this.setStatus('Starting your new Leo…');
    let result;
    try {
      result = await this.api.launch(item.id);
    } catch (e) {
      this.setStatus("Couldn't reach LlamaPress. Please try again in a moment.");
      return;
    }
    if (!result.ok) {
      this.showError(result.body);
      return;
    }
    const instance = result.body.instance || {};
    this.setStatus(launchedText(instance), instance.dashboard_url || '', 'Open your dashboard');
  }

  // ------------------------------------------------------------------ delete

  async confirmDelete(item, opener) {
    const yes = await this.confirm({
      title: `Delete "${item.name}"?`,
      text: 'This saved version will be gone for good. Your app as it is now is not affected.',
      confirmLabel: 'Delete version',
      opener,
    });
    if (!yes) return;
    let result;
    try {
      result = await this.api.remove(item.id);
    } catch (e) {
      this.setStatus("Couldn't reach LlamaPress. Please try again in a moment.");
      return;
    }
    if (!result.ok) {
      this.showError(result.body);
      return;
    }
    this.setStatus(`"${item.name}" deleted.`);
    await this.refresh();
  }

  stop() {
    clearTimeout(this.savingTimer);
  }
}
