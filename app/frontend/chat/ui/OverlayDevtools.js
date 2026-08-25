/**
 * `window.leoAds` — console handle for the building overlay + its promo slot.
 *
 * Exercising the overlay normally means sending Leo a real message and waiting
 * for a real build, and checking a rotation means waiting out the mothership's
 * cadence (3 minutes in production). This makes both instant:
 *
 *   leoAds.show()      overlay up now, promos load, no agent turn
 *   leoAds.next()      jump to the next promo instead of waiting
 *   leoAds.plan()      force the todo-list layout (promo shrinks under the box)
 *   leoAds.question()  force the question layout (promo must disappear)
 *   leoAds.ads()       table of what /api/overlay-ads is serving right now
 *
 * Grants nothing new: `window.chatApp` is already global, so everything here is
 * reachable from the console regardless. It exists so nobody has to keep a block
 * of code on their clipboard.
 *
 * `show()`/`hide()`/`toggle()` use public methods and are stable. `next()`,
 * `plan()`, `question()` and `building()` poke at overlay internals — they're
 * debug-only, and a chat mutation can flip the forced mode back out from under
 * you (the plan mirror owns the mode in real use).
 */

const OVERLAY_TITLE = 'Your App is Building!';

/**
 * Install the helper on `win`. Called from the IframeManager constructor with
 * the live manager, so it never depends on `window.chatApp` being assigned yet.
 */
export function installOverlayDevtools(iframeManager, win = globalThis) {
  if (!win) return null;

  const im = () => iframeManager;

  const leoAds = {
    im,

    /** Put the overlay up on demand. Tears down any existing one first, since
     *  createStreamingOverlay is a no-op while one is already on screen. */
    show(text = OVERLAY_TITLE) {
      im().removeStreamingOverlay();
      im().createStreamingOverlay({ showCloseButton: true, text });
      return `overlay up — leoAds.next() to skip ahead, leoAds.hide() to dismiss`;
    },

    hide() { im().removeStreamingOverlay(); },

    toggle() {
      return (win.document && win.document.getElementById('streamingOverlay'))
        ? leoAds.hide()
        : leoAds.show();
    },

    /** Skip the rotation wait. No-op when there's one promo or none. */
    next() { im()._overlayAdRotator?.next(); },

    plan()     { im()._setOverlayMode?.('plan'); },
    question() { im()._setOverlayMode?.('question'); },
    building() { im()._setOverlayMode?.('building'); },

    /** Which promo is on screen, why, and where the layout currently stands.
     *  `verdict` is the reason the policy gave — 'ok', 'no-ads',
     *  'disabled-by-policy', or 'cooldown:<n>s-left' — so "where's my ad?" is
     *  answerable without reading code. */
    status() {
      const r = im()._overlayAdRotator;
      return {
        overlay: !!(win.document && win.document.getElementById('streamingOverlay')),
        mode: im()._overlayMode || null,
        ads: r ? r.ads.length : 0,
        showing: r?.currentAd?.id || null,
        rotateSeconds: r?.rotateSeconds ?? null,
        verdict: im()._overlayAdVerdict || null,
        policy: im()._overlayAdPolicy || null,
        variant: im()._overlayAdVariant || null,
        pendingDelay: !!im()._overlayAdDelayTimer,
      };
    },

    /** Clear the per-browser cooldown so the next show() isn't suppressed. */
    resetCooldown() {
      try { win.localStorage?.removeItem('llamabot.overlayAds.lastShownAt'); } catch (e) { /* private mode */ }
      return 'cooldown cleared';
    },

    /** What policy the endpoint is handing this browser right now. */
    policy() {
      return win.fetch('/api/overlay-ads')
        .then((r) => r.json())
        .then((b) => ({ policy: b.policy, variant: b.variant, ads: b.ads.length }));
    },

    /** What the endpoint is serving right now (bypasses whatever is on screen). */
    ads() {
      return win.fetch('/api/overlay-ads')
        .then((r) => r.json())
        .then((body) => {
          win.console?.table?.(body.ads.map(({ id, height, html }) => ({
            id, height, bytes: html.length,
          })));
          return body;
        });
    },
  };

  win.leoAds = leoAds;
  return leoAds;
}
