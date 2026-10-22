/**
 * How hard to warn before a Discard (0.7.15).
 *
 * A customer who never made a checkpoint discarded his whole app (430 files,
 * 22 controllers, 26 models) after the same one-line confirm() a single edit
 * gets. Big or never-committed change sets now get a modal that names the size
 * and offers "Save a checkpoint instead" first.
 *
 * English only: the chat UI has no i18n mechanism to route these through.
 */

export const STRONG_WARNING_FILE_COUNT = 50;

/** True when a discard could take out the app itself, not just an edit. */
export function needsStrongWarning(changes) {
  return (changes?.total_count || 0) >= STRONG_WARNING_FILE_COUNT
    || Boolean(changes?.untracked_controllers)
    || Boolean(changes?.untracked_models);
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/** "This will remove 430 files, including 22 controllers and 26 models. ..." */
export function discardWarningText(changes) {
  const parts = [];
  if (changes.controller_count) parts.push(plural(changes.controller_count, 'controller'));
  if (changes.model_count) parts.push(plural(changes.model_count, 'model'));
  const including = parts.length ? `, including ${parts.join(' and ')}` : '';
  return `This will remove ${plural(changes.total_count, 'file')}${including}. `
    + 'Your app will stop working until you undo.';
}

/**
 * Show the strong warning. Resolves 'checkpoint' (the primary button),
 * 'discard' (the secondary one), or 'cancel' (closed some other way).
 */
export function confirmLargeDiscard(changes) {
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'update-modal-overlay discard-warning-overlay';
    overlay.innerHTML = `
      <div class="update-modal-card discard-warning-card" role="dialog" aria-modal="true">
        <div class="update-modal-head">
          <span class="update-modal-badge warn"><i class="fa-solid fa-triangle-exclamation"></i></span>
          <h2>Discard your whole app?</h2>
        </div>
        <p class="discard-warning-text"></p>
        <p class="discard-warning-hint">A backup is saved first, so you can undo this.
          Saving a checkpoint keeps your work instead.</p>
        <div class="discard-warning-actions">
          <button class="discard-warning-checkpoint"><i class="fa-solid fa-floppy-disk"></i> Save a checkpoint instead</button>
          <button class="discard-warning-discard"></button>
        </div>
      </div>`;
    overlay.querySelector('.discard-warning-text').textContent = discardWarningText(changes);
    overlay.querySelector('.discard-warning-discard').textContent =
      `Discard ${plural(changes.total_count, 'file')}`;

    const close = (choice) => {
      overlay.remove();
      document.removeEventListener('keydown', onKey);
      resolve(choice);
    };
    const onKey = (e) => { if (e.key === 'Escape') close('cancel'); };

    overlay.querySelector('.discard-warning-checkpoint').onclick = () => close('checkpoint');
    overlay.querySelector('.discard-warning-discard').onclick = () => close('discard');
    overlay.onclick = (e) => { if (e.target === overlay) close('cancel'); };
    document.addEventListener('keydown', onKey);

    document.body.appendChild(overlay);
    overlay.querySelector('.discard-warning-checkpoint').focus();
  });
}
