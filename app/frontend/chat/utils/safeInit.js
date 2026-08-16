/**
 * Guards for start-up and send-time calls into other chat modules.
 *
 * chat.html cache-busts only the entry point (`index.js?v=N`); the 36 modules
 * it imports carry no version stamp, so a browser can end up running a new
 * index.js against an older cached module. On leo-tama (0.7.0) that produced
 * `this.fileAttachmentManager.initAssetModal is not a function` inside
 * initComponents — which threw before the rest of init ran, leaving the chat
 * panel dead for the whole page load.
 *
 * Version-stamping the whole module graph is the real fix. These helpers are
 * the containment: a skewed module costs one feature, not the panel.
 */

/**
 * Run one initialization step, surviving a failure inside it.
 * @param {string} label - what is being initialized, for the log line
 * @param {Function} step - the work
 * @param {Function} [log] - injected for tests
 * @returns {boolean} whether the step completed
 */
export function safeInit(label, step, log = console.error) {
  try {
    step();
    return true;
  } catch (e) {
    log(`[ChatApp] init step "${label}" failed — continuing without it:`, e);
    return false;
  }
}

/**
 * Read the currently selected page elements, whatever state the selector is in.
 *
 * Selected elements are optional context on a message, so a broken selector
 * must never block a send. `this.elementSelector?.getSelectedElements()` does
 * not cover this: optional chaining guards a null selector, not a live object
 * whose method is missing.
 *
 * @param {object|null|undefined} elementSelector
 * @returns {Array}
 */
export function selectedElementsOf(elementSelector) {
  if (!elementSelector || typeof elementSelector.getSelectedElements !== 'function') {
    return [];
  }

  try {
    const selected = elementSelector.getSelectedElements();
    return Array.isArray(selected) ? selected : [];
  } catch (e) {
    console.error('[ChatApp] element selector failed; sending without it:', e);
    return [];
  }
}
