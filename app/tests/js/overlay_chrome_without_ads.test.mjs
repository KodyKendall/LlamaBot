// The building overlay must be UNCHANGED when there is no promo to show.
//
// The jumbotron shrinks the status block into a compact top-left pill so the
// promo owns the pane. That trade only makes sense when a promo is actually on
// screen: if the mothership is down, unconfigured, or the policy suppresses the
// slot, the user should see the pre-jumbotron overlay exactly as it was — big
// centered animation under a big title.
//
// So the property under test is "chrome follows the promo, not the feature":
// no promo → classic proportions; promo mounted → compact ones.
//
// Drives the real IframeManager against a stub DOM, like iframe_session_restore.

import assert from 'node:assert/strict';
import test from 'node:test';

class FakeClassList {
  constructor(initial = []) { this._set = new Set(initial); }
  add(...n) { n.forEach((x) => this._set.add(x)); }
  remove(...n) { n.forEach((x) => this._set.delete(x)); }
  contains(n) { return this._set.has(n); }
}

/** Enough of a DOM node for the overlay: styles, children, and re-parenting. */
class FakeElement {
  constructor(tag = 'div', { classes = [], dataset = {} } = {}) {
    this.tagName = tag;
    this.classList = new FakeClassList(classes);
    this.dataset = dataset;
    this.style = {};
    this.attrs = {};
    this.children = [];
    this.parentNode = null;
    this.src = '';
    this.value = '';
    this.clientWidth = 1200;
    this._html = '';
  }
  get firstChild() { return this.children[0] || null; }
  _detach(c) {
    if (c.parentNode) {
      const i = c.parentNode.children.indexOf(c);
      if (i >= 0) c.parentNode.children.splice(i, 1);
    }
  }
  appendChild(c) { this._detach(c); this.children.push(c); c.parentNode = this; return c; }
  insertBefore(c, ref) {
    this._detach(c);
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i >= 0) this.children.splice(i, 0, c); else this.children.push(c);
    c.parentNode = this;
    return c;
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  set innerHTML(v) { this._html = v; if (v === '') this.children = []; }
  get innerHTML() { return this._html; }
}

/** The chat.html container + globals IframeManager and the overlay read. */
function makeEnv({ payload = null, ok = true } = {}) {
  const els = {
    'live-site-frame': new FakeElement('iframe', { classes: ['content-iframe', 'active'] }),
    'url-input': new FakeElement('input'),
    'url-dropdown': new FakeElement(),
  };
  const container = {
    querySelector(sel) {
      const m = /\[data-llamabot="([^"]+)"\]/.exec(sel);
      return (m && els[m[1]]) || null;
    },
    querySelectorAll(sel) {
      if (sel === '.content-iframe') return [els['live-site-frame']];
      return [];
    },
  };

  const browserContent = new FakeElement('div', { classes: ['browser-content'] });

  globalThis.window = {
    location: { protocol: 'https:', host: 'box.llamapress.ai', pathname: '/chat', search: '', hash: '' },
    history: { replaceState() {} },
    localStorage: { getItem: () => null, setItem: () => {} },
    LLAMABOT_USER_ROLE: 'engineer',
    addEventListener() {},
  };
  globalThis.localStorage = window.localStorage;
  globalThis.document = {
    addEventListener() {},
    createElement: (tag) => new FakeElement(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: text, children: [] }),
    getElementById: () => null,
    querySelector(sel) {
      if (sel === '.browser-content') return browserContent;
      // Pretend the lottie player script is already on the page.
      if (sel.includes('lottie-player')) return new FakeElement('script');
      return null; // no message history → the plan mirror stays out of the way
    },
    head: new FakeElement('head'),
  };
  // Real timers would keep the test process alive on the 20s tip rotation.
  globalThis.setInterval = () => 1;
  globalThis.clearInterval = () => {};
  globalThis.requestAnimationFrame = () => 1;
  globalThis.ResizeObserver = class { observe() {} disconnect() {} };
  globalThis.MutationObserver = class { observe() {} disconnect() {} };
  globalThis.fetch = async () => ({
    ok,
    json: async () => payload,
  });

  return { container, browserContent };
}

const { IframeManager } = await import('../../frontend/chat/ui/IframeManager.js');

/** Named handles on the overlay's children, in DOM order. */
function overlayParts(browserContent) {
  const overlay = browserContent.children.find((c) => c.attrs?.id === undefined && c.style.zIndex === '10')
    || browserContent.children[browserContent.children.length - 1];
  const findById = (id) => {
    const walk = (node) => {
      for (const c of node.children) {
        if (c.id === id) return c;
        const hit = walk(c);
        if (hit) return hit;
      }
      return null;
    };
    return walk(overlay);
  };
  const adSlot = findById('overlayAdSlot');
  const lottie = findById('lottieAnimation');
  return { overlay, adSlot, lottie, title: lottie.parentNode };
}

/** Build the overlay and let the (already-resolved) ad fetch settle. */
async function showOverlay(env) {
  const mgr = new IframeManager(env.container);
  mgr.createStreamingOverlay({ text: 'Your App is Building!' });
  await new Promise((r) => setImmediate(r));   // fetch → policy → mount
  return mgr;
}

const ADS_PAYLOAD = {
  ads: [{ id: 'promo', html: '<b>Promo</b>', height: 140 }],
  rotate_seconds: 180,
  // No hold-back, so the promo is up by the time we assert.
  policy: { enabled: true, show_after_seconds: 0, modes: ['building', 'plan'], min_interval_seconds: 0 },
};

// ---------------------------------------------------------------------------

test('no promo → the overlay looks exactly like it did before the jumbotron', async () => {
  const env = makeEnv({ payload: { ads: [] } });
  await showOverlay(env);
  const { overlay, adSlot, lottie } = overlayParts(env.browserContent);

  assert.equal(adSlot.style.display, 'none', 'an empty payload must not leave an empty slot on screen');
  // The animation is its own full-width block under the pill, not a row item.
  assert.equal(lottie.parentNode, overlay);
  assert.equal(lottie.style.width, '100%');
  assert.equal(lottie.children[0].style.width, '240px', 'the big building animation is back');
  assert.equal(overlay.children[0].style.justifyContent, 'center', 'the status pill is centered again');
});

test('no promo because the mothership is down → still the classic overlay', async () => {
  const env = makeEnv({ ok: false });
  await showOverlay(env);
  const { overlay, adSlot, lottie } = overlayParts(env.browserContent);

  assert.equal(adSlot.style.display, 'none');
  assert.equal(lottie.parentNode, overlay);
  assert.equal(lottie.children[0].style.width, '240px');
});

test('a mounted promo shrinks the status block into the compact pill', async () => {
  const env = makeEnv({ payload: ADS_PAYLOAD });
  await showOverlay(env);
  const { overlay, adSlot, lottie } = overlayParts(env.browserContent);

  assert.equal(adSlot.style.display, 'block');
  assert.notEqual(lottie.parentNode, overlay, 'the animation moves into the pill beside the title');
  assert.equal(lottie.parentNode.firstChild, lottie, 'and sits ahead of the title/tip stack');
  assert.equal(lottie.children[0].style.width, '58px', 'a small ball, so the promo owns the pane');
});

test('a promo suppressed by policy leaves the classic overlay untouched', async () => {
  const env = makeEnv({
    payload: { ...ADS_PAYLOAD, policy: { ...ADS_PAYLOAD.policy, enabled: false } },
  });
  await showOverlay(env);
  const { overlay, adSlot, lottie } = overlayParts(env.browserContent);

  assert.equal(adSlot.style.display, 'none');
  assert.equal(lottie.parentNode, overlay);
  assert.equal(lottie.children[0].style.width, '240px');
});
