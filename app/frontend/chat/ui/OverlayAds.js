/**
 * Overlay Ads — the "jumbotron" promo slot in the building overlay.
 *
 * The creative lives on the mothership (`GET /api/overlay-ads` proxies it), so a
 * promo can be written/edited there and picked up on the next build with no
 * instance deploy. This module owns three things and nothing else:
 *
 *   1. Fetching the slot's payload, fail-open (any error → no slot at all).
 *   2. Rendering each snippet inside a SANDBOXED iframe.
 *   3. Rotating between snippets on the mothership's cadence.
 *
 * ## Why an iframe and not innerHTML
 *
 * The snippet is remote HTML rendered inside the signed-in chat page. Dropping it
 * into the chat DOM would be self-inflicted XSS — one bad or compromised promo
 * would own the user's session. Each snippet instead goes into an iframe with
 * `sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"`:
 * `allow-scripts` WITHOUT `allow-same-origin` puts the frame in an opaque origin,
 * so a promo can animate itself but cannot read the parent DOM, cookies, or
 * localStorage. `allow-popups*` is what lets a promo's link actually open a tab.
 * **Never add `allow-same-origin` here** — with `allow-scripts` it cancels the
 * sandbox entirely.
 */

/** Sandbox flags for the ad frame. See the module header before changing these. */
export const AD_SANDBOX = 'allow-scripts allow-popups allow-popups-to-escape-sandbox';

/** Fallback cadence if the payload omits one; the backend clamps the real value. */
export const DEFAULT_ROTATE_SECONDS = 180;

/**
 * Fallback display policy, mirroring the backend's `DEFAULT_POLICY`. Only used
 * when the endpoint is unreachable — a served payload always carries a policy,
 * already merged and clamped server-side.
 */
export const DEFAULT_POLICY = Object.freeze({
  enabled: true,
  show_after_seconds: 5,
  modes: ['building', 'plan'],
  min_interval_seconds: 0,
});

/** Where the per-browser cooldown timestamp lives. */
export const LAST_SHOWN_KEY = 'llamabot.overlayAds.lastShownAt';

/** Cross-fade duration, matched by the CSS transition on the frame. */
const FADE_MS = 400;

/**
 * Wrap a promo snippet in a full document.
 *
 * `<base target="_blank">` is the important part: the frame is sandboxed, so a
 * link that tried to navigate in place would go nowhere useful — every promo
 * link should leave for a real tab instead. The reset keeps a snippet from
 * inheriting nothing and rendering with the UA's default 8px body margin.
 */
export function adDocument(html) {
  return `<!doctype html><html><head><meta charset="utf-8">`
    + `<base target="_blank" rel="noopener noreferrer">`
    + `<style>html,body{margin:0;padding:0;height:100%;`
    + `font-family:Arial,Helvetica,sans-serif;color:#fff;background:transparent;}`
    // The slot is short beside a todo list but fills the whole pane while Leo is
    // starting up, so the same snippet has to look right at 140px AND at ~500px.
    // Stretching the body's top-level element(s) to the frame means creative
    // authored as a fixed-height banner just grows its background instead of
    // floating in dead space. An author who wants natural height opts out with
    // `flex:none` on their root element.
    + `body{display:flex;flex-direction:column;}body>*{flex:1 1 auto;min-height:0;}`
    + `*{box-sizing:border-box;}a{color:inherit;}</style></head>`
    + `<body>${html}</body></html>`;
}

/**
 * Fetch the promo payload. Always resolves — never rejects — to
 * `{ ads: [], rotateSeconds }` on any failure, so a dead endpoint just means the
 * overlay looks exactly like it did before this feature existed.
 */
export async function loadOverlayAds(fetchImpl = globalThis.fetch) {
  const empty = { ads: [], rotateSeconds: DEFAULT_ROTATE_SECONDS, policy: { ...DEFAULT_POLICY }, variant: null };
  try {
    const response = await fetchImpl('/api/overlay-ads', { credentials: 'same-origin' });
    if (!response || !response.ok) return empty;
    const body = await response.json();
    const ads = Array.isArray(body?.ads)
      ? body.ads.filter((ad) => ad && typeof ad.html === 'string' && ad.html.trim())
      : [];
    const rotate = Number(body?.rotate_seconds);
    return {
      ads,
      rotateSeconds: Number.isFinite(rotate) && rotate > 0 ? rotate : DEFAULT_ROTATE_SECONDS,
      // Server-merged and clamped; only fall back if the field is missing entirely.
      policy: { ...DEFAULT_POLICY, ...(body?.policy || {}) },
      variant: body?.variant || null,
    };
  } catch (e) {
    return empty;
  }
}

/**
 * Decide whether this build gets a promo at all, and after how long.
 *
 * All of the actual product judgement here is the mothership's — this only
 * applies what it sent. Returns `{ show, delayMs, reason }`; `reason` is what
 * `leoAds.status()` surfaces so "why is there no ad?" is answerable without
 * reading code.
 *
 * `now` and `storage` are injected so the cooldown is testable without waiting
 * and without a real localStorage.
 */
export function evaluatePolicy({
  ads = [],
  policy = DEFAULT_POLICY,
  now = Date.now(),
  storage = globalThis.localStorage,
} = {}) {
  if (!ads.length) return { show: false, delayMs: 0, reason: 'no-ads' };
  if (policy.enabled === false) return { show: false, delayMs: 0, reason: 'disabled-by-policy' };

  const cooldown = Number(policy.min_interval_seconds) || 0;
  if (cooldown > 0) {
    // localStorage throws in some embedded/private contexts — a cooldown we
    // can't read is not a reason to suppress the promo, so fail open.
    let lastShown = 0;
    try { lastShown = Number(storage?.getItem(LAST_SHOWN_KEY)) || 0; } catch (e) { lastShown = 0; }
    const elapsed = (now - lastShown) / 1000;
    if (lastShown && elapsed < cooldown) {
      return { show: false, delayMs: 0, reason: `cooldown:${Math.ceil(cooldown - elapsed)}s-left` };
    }
  }

  const delaySeconds = Math.max(0, Number(policy.show_after_seconds) || 0);
  return { show: true, delayMs: delaySeconds * 1000, reason: 'ok' };
}

/** Stamp "a promo was shown just now" for the cooldown. Never throws. */
export function recordShown(now = Date.now(), storage = globalThis.localStorage) {
  try { storage?.setItem(LAST_SHOWN_KEY, String(now)); } catch (e) { /* private mode */ }
}

/** Is `mode` one the policy allows a promo in? */
export function policyAllowsMode(policy, mode) {
  const modes = Array.isArray(policy?.modes) ? policy.modes : DEFAULT_POLICY.modes;
  return modes.includes(mode);
}

/**
 * Renders one promo at a time into `slotEl` and cycles through the rest.
 *
 * Timers are injected so the rotation is testable without waiting three minutes.
 */
export class OverlayAdRotator {
  constructor({
    ads = [],
    rotateSeconds = DEFAULT_ROTATE_SECONDS,
    doc = globalThis.document,
    timers = globalThis,
    onAdChange = null,
  } = {}) {
    this.ads = ads;
    this.rotateSeconds = rotateSeconds;
    this.doc = doc;
    this.timers = timers;
    // Fired with the ad that just became visible. The rotator deliberately does
    // NOT size its own slot: how much room a promo gets is the overlay's call
    // (it fills the pane while Leo is starting up, but yields to the todo list
    // once there's a plan), so the payload's `height` is a hint the overlay
    // applies, not something this class imposes.
    this.onAdChange = onAdChange;
    this.currentAd = null;
    this.index = 0;
    this.frame = null;
    this.slot = null;
    this._interval = null;
    this._fadeTimeout = null;
  }

  /**
   * Build the frame, show the first promo, and start rotating.
   * No-op (returns false) with no ads or no slot — the caller keeps the slot
   * hidden in that case, which is the normal state on an unconfigured box.
   */
  mount(slotEl) {
    if (!slotEl || !this.ads.length) return false;
    this.slot = slotEl;

    const frame = this.doc.createElement('iframe');
    frame.setAttribute('sandbox', AD_SANDBOX);
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.setAttribute('title', 'LlamaPress promo');
    frame.style.width = '100%';
    frame.style.height = '100%';
    frame.style.border = 'none';
    frame.style.display = 'block';
    frame.style.opacity = '1';
    frame.style.transition = `opacity ${FADE_MS}ms ease`;
    this.frame = frame;

    slotEl.innerHTML = '';
    slotEl.appendChild(frame);
    this._render(0);

    // A single promo is a static banner, not a carousel — don't burn a timer.
    if (this.ads.length > 1) {
      this._interval = this.timers.setInterval(
        () => this.next(),
        this.rotateSeconds * 1000,
      );
    }
    return true;
  }

  /** Advance to the next promo, wrapping around, with a cross-fade. */
  next() {
    if (!this.frame || this.ads.length < 2) return;
    const nextIndex = (this.index + 1) % this.ads.length;
    this.frame.style.opacity = '0';
    this._fadeTimeout = this.timers.setTimeout(() => {
      this._render(nextIndex);
      if (this.frame) this.frame.style.opacity = '1';
    }, FADE_MS);
  }

  /** Point the frame at ad `i` and tell the overlay which ad is now showing. */
  _render(i) {
    const ad = this.ads[i];
    if (!ad || !this.frame) return;
    this.index = i;
    this.currentAd = ad;
    this.frame.srcdoc = adDocument(ad.html);
    this.onAdChange?.(ad);
  }

  /** Stop rotating and drop the frame. Safe to call more than once. */
  stop() {
    if (this._interval) {
      this.timers.clearInterval(this._interval);
      this._interval = null;
    }
    if (this._fadeTimeout) {
      this.timers.clearTimeout(this._fadeTimeout);
      this._fadeTimeout = null;
    }
    if (this.slot) this.slot.innerHTML = '';
    this.frame = null;
    this.slot = null;
    this.currentAd = null;
  }
}
