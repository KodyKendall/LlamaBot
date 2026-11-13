/**
 * What the History panel says about saving (LP-46).
 *
 * Built only from GET /api/checkpoints/status, whose `clean` is live git status —
 * so the panel can never claim "saved" after a save that failed. There is no
 * "unsaved" count any more: a user once saw "669 unsaved changes" for work that
 * was safe on disk, and the only button on offer was Discard.
 */

export const SAVED_TEXT = 'All changes saved';
export const WORKING_TEXT = 'Saving after Leo finishes…';
export const NOT_SAVED_TEXT = "Some recent changes aren't in a save point yet";
export const DOT_LABEL = 'Changes not yet saved';
// LP-49: the user turned auto-save off on purpose, so unsaved changes are expected.
export const AUTOSAVE_OFF_TEXT = 'Auto-save is off';

/** "just now", "2 min ago", "3 hours ago", "4 days ago", else the date. */
export function relativeTime(iso, now = new Date()) {
  if (!iso) return '';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '';
  const mins = Math.floor((now - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? '' : 's'} ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} day${days === 1 ? '' : 's'} ago`;
  return then.toLocaleDateString();
}

/**
 * @returns {{state: 'unknown'|'saved'|'working'|'not-saved'|'off', text: string, time: string,
 *            detail: string, showSaveNow: boolean, showDot: boolean, autosaveOff: boolean}}
 */
export function describeSaveStatus(status, now = new Date()) {
  const base = { time: '', detail: '', showSaveNow: false, showDot: false, autosaveOff: false };
  if (!status) return { ...base, state: 'unknown', text: 'Checking save status…' };

  // Off (LP-49): calm and factual, never the "not saved" dot. Only an explicit
  // false counts, so an older backend without the field keeps LP-46's behaviour.
  if (status.autosave_enabled === false) {
    const ago = relativeTime(status.last_saved_at, now);
    return {
      ...base,
      state: 'off',
      text: AUTOSAVE_OFF_TEXT,
      time: ago ? ` · last saved ${ago}` : '',
      detail: status.last_save_error || '',
      showSaveNow: !status.clean,
      autosaveOff: true,
    };
  }

  // A turn that hasn't touched anything yet is still "saved"; one mid-edit is not.
  if (!status.clean && status.turn_in_progress) {
    return { ...base, state: 'working', text: WORKING_TEXT };
  }
  if (status.clean) {
    const ago = relativeTime(status.last_saved_at, now);
    return { ...base, state: 'saved', text: SAVED_TEXT, time: ago ? ` · ${ago}` : '' };
  }
  return {
    ...base,
    state: 'not-saved',
    text: NOT_SAVED_TEXT,
    detail: status.last_save_error || '',
    showSaveNow: true,
    showDot: true,
  };
}

/** "This will undo changes to 4 files since your last save. …" */
export function undoChangesConfirmText(count) {
  const files = `${count} file${count === 1 ? '' : 's'}`;
  // Discard snapshots first (LP-7), so "can't be undone" would be false — and
  // scaring people into keeping a change is how the old badge did harm.
  return `This will undo changes to ${files} since your last save.\n\n`
    + 'A backup is kept, so you can bring them back with "Undo discard".';
}
