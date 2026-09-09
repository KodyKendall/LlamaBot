/**
 * Decides whether a captured preview error belongs ENTIRELY to a browser
 * extension (MetaMask, password managers, request interceptors) rather than to
 * the user's app.
 *
 * Extensions inject their scripts into the main world of the previewed
 * document, so they share `window` with the Rails app and their throws fire the
 * app's own error listeners. The postMessage then arrives here from the correct
 * Rails origin, indistinguishable from a real app error — until Leo spends a
 * paragraph of its answer reassuring the user about MetaMask.
 *
 * The Rails overlay (llamapress/extension_error_filter.js) drops these at the
 * source, which is the half that matters; this is the same check on the
 * receiving side, so a box whose overlay lags the image still behaves. The two
 * copies are deliberate — the chat bundle and the Rails overlay ship in
 * different images and share no imports. Keep them in step.
 *
 * The rule is NOT "contains chrome-extension://". An extension that
 * monkey-patches window.fetch ends up as the TOP frame of a stack whose lower
 * frames are the app's own code, and that error is a real app problem. So: drop
 * only when at least one frame is extension code AND no frame comes from an
 * http(s) origin. Frames with no URL (native, <anonymous>, eval) count neither
 * way, which keeps a bare "Script error." — a browser CORS rule, not an
 * extension — visible.
 */

const EXTENSION_SCHEME = /\b(?:chrome|moz|safari-web|safari|ms-browser)-extension:\/\//;
const HTTP_URL = /\bhttps?:\/\/[^\s)]+/g;

/**
 * @param {{message?: string, stack?: string, filename?: string}} [error]
 * @returns {boolean} true when every located frame is extension code
 */
export function isExtensionOnly({ message, stack, filename } = {}) {
  try {
    const text = [filename, message, stack].filter(Boolean).join('\n');
    if (!EXTENSION_SCHEME.test(text)) return false;
    return (text.match(HTTP_URL) || []).length === 0;
  } catch {
    return false;  // never hide a real error because this threw
  }
}
