/**
 * "Undo discard" card above the chat input (0.7.15).
 *
 * The panel's own Undo banner disappears with the panel, and after a big
 * discard the app preview is a 404, so people never went back there. This card
 * sits in the chat itself and comes back after a reload (the server keeps the
 * discard "pending") until the user undoes or dismisses it. It lives outside
 * the message list, so switching chats does not wipe it.
 */

export class DiscardUndoCard {
  constructor({ onUndo, onDismiss }) {
    this.onUndo = onUndo;
    this.onDismiss = onDismiss;
    this.node = null;
  }

  /** Show the newest pending discard from a /discard/backups list, or nothing. */
  render(backups) {
    const backup = (backups || []).find((b) => b.pending);
    if (!backup) {
      this.hide();
      return;
    }

    const chatSection = document.querySelector('.chat-section');
    const inputArea = document.querySelector('.input-area');
    if (!chatSection) return;

    this.hide();
    const card = document.createElement('div');
    card.className = 'discard-undo-card';

    const message = document.createElement('div');
    message.className = 'discard-undo-card-text';
    const files = backup.file_count === 1 ? '1 file' : `${backup.file_count} files`;
    message.textContent = `You discarded ${files}. Your app may not work until you undo.`;

    const undo = document.createElement('button');
    undo.className = 'discard-undo-card-undo';
    undo.textContent = 'Undo discard';
    undo.onclick = () => {
      undo.disabled = true;
      this.onUndo(backup.ref);
    };

    const dismiss = document.createElement('button');
    dismiss.className = 'discard-undo-card-dismiss';
    dismiss.textContent = '×';
    dismiss.title = 'Dismiss (the backup is kept)';
    dismiss.onclick = () => {
      this.hide();
      this.onDismiss(backup.ref);
    };

    card.appendChild(message);
    card.appendChild(undo);
    card.appendChild(dismiss);
    chatSection.insertBefore(card, inputArea);
    this.node = card;
  }

  hide() {
    this.node?.remove();
    this.node = null;
  }
}
