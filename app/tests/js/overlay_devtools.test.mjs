// window.leoAds — the console handle for driving the building overlay.
//
// It's a convenience wrapper, so the properties worth pinning are the ones that
// would silently rot: that it lands on window at all, that show() tears down an
// existing overlay first (createStreamingOverlay no-ops while one is up), and
// that the mode shortcuts reach _setOverlayMode.

import assert from 'node:assert/strict';
import test from 'node:test';

import { installOverlayDevtools } from '../../frontend/chat/ui/OverlayDevtools.js';

/** Records what the helper asked the IframeManager to do. */
function stubManager() {
  const calls = [];
  return {
    calls,
    _overlayMode: 'building',
    _overlayAdRotator: {
      ads: [{ id: 'a' }, { id: 'b' }],
      rotateSeconds: 180,
      currentAd: { id: 'a' },
      next() { calls.push('rotator.next'); },
    },
    _overlayAdVerdict: 'ok',
    _overlayAdPolicy: { enabled: true, modes: ['building', 'plan'] },
    _overlayAdVariant: 'arm-b',
    _overlayAdDelayTimer: null,
    removeStreamingOverlay() { calls.push('remove'); },
    createStreamingOverlay(opts) { calls.push(['create', opts]); },
    _setOverlayMode(mode) { calls.push(['mode', mode]); },
  };
}

function stubWindow(overlayPresent = false) {
  return {
    document: { getElementById: (id) => (overlayPresent && id === 'streamingOverlay' ? {} : null) },
    console: { table() {} },
    fetch: async () => ({ json: async () => ({ ads: [{ id: 'a', height: 140, html: '<b>x</b>' }] }) }),
  };
}

test('installs itself on window', () => {
  const win = stubWindow();
  const returned = installOverlayDevtools(stubManager(), win);
  assert.equal(win.leoAds, returned);
  assert.equal(typeof win.leoAds.show, 'function');
});

test('show() removes any existing overlay before creating one', () => {
  // createStreamingOverlay early-returns while an overlay is on screen, so
  // without the remove first, a second show() would silently do nothing.
  const im = stubManager();
  const leoAds = installOverlayDevtools(im, stubWindow());
  leoAds.show();

  assert.equal(im.calls[0], 'remove');
  assert.equal(im.calls[1][0], 'create');
  assert.equal(im.calls[1][1].text, 'Your App is Building!');
  assert.equal(im.calls[1][1].showCloseButton, true);
});

test('show() accepts a custom title', () => {
  const im = stubManager();
  installOverlayDevtools(im, stubWindow()).show('Custom');
  assert.equal(im.calls[1][1].text, 'Custom');
});

test('toggle() hides when an overlay is up and shows when it is not', () => {
  const up = stubManager();
  installOverlayDevtools(up, stubWindow(true)).toggle();
  assert.deepEqual(up.calls, ['remove']);

  const down = stubManager();
  installOverlayDevtools(down, stubWindow(false)).toggle();
  assert.equal(down.calls[1][0], 'create');
});

test('mode shortcuts reach _setOverlayMode', () => {
  const im = stubManager();
  const leoAds = installOverlayDevtools(im, stubWindow());
  leoAds.plan(); leoAds.question(); leoAds.building();

  assert.deepEqual(im.calls, [['mode', 'plan'], ['mode', 'question'], ['mode', 'building']]);
});

test('next() drives the rotator', () => {
  const im = stubManager();
  installOverlayDevtools(im, stubWindow()).next();
  assert.deepEqual(im.calls, ['rotator.next']);
});

test('the shortcuts no-op instead of throwing when no overlay is up', () => {
  // _setOverlayMode and _overlayAdRotator are null between builds; hitting the
  // helper then is the normal case, not a mistake.
  const im = { removeStreamingOverlay() {}, createStreamingOverlay() {},
               _setOverlayMode: null, _overlayAdRotator: null, _overlayMode: null };
  const leoAds = installOverlayDevtools(im, stubWindow());

  leoAds.next(); leoAds.plan(); leoAds.question(); leoAds.building();
  assert.deepEqual(leoAds.status(), {
    overlay: false, mode: null, ads: 0, showing: null, rotateSeconds: null,
    verdict: null, policy: null, variant: null, pendingDelay: false,
  });
});

test('status() reports what is on screen, and WHY', () => {
  // `verdict` is the whole point: when no promo shows, this is what tells you
  // whether it was the cooldown, the kill switch, or simply no creative.
  const leoAds = installOverlayDevtools(stubManager(), stubWindow(true));
  assert.deepEqual(leoAds.status(), {
    overlay: true, mode: 'building', ads: 2, showing: 'a', rotateSeconds: 180,
    verdict: 'ok', policy: { enabled: true, modes: ['building', 'plan'] },
    variant: 'arm-b', pendingDelay: false,
  });
});

test('status() surfaces a suppression reason instead of just an absent ad', () => {
  const im = stubManager();
  im._overlayAdRotator = null;
  im._overlayAdVerdict = 'cooldown:1799s-left';
  const leoAds = installOverlayDevtools(im, stubWindow(true));

  assert.equal(leoAds.status().verdict, 'cooldown:1799s-left');
  assert.equal(leoAds.status().showing, null);
});

test('resetCooldown clears the stored timestamp', () => {
  const win = stubWindow();
  const store = { 'llamabot.overlayAds.lastShownAt': '123' };
  win.localStorage = { removeItem: (k) => { delete store[k]; } };
  installOverlayDevtools(stubManager(), win).resetCooldown();

  assert.equal('llamabot.overlayAds.lastShownAt' in store, false);
});

test('resetCooldown survives a hostile localStorage', () => {
  const win = stubWindow();
  win.localStorage = { removeItem() { throw new Error('denied'); } };
  assert.doesNotThrow(() => installOverlayDevtools(stubManager(), win).resetCooldown());
});

test('ads() returns what the endpoint is serving', async () => {
  const leoAds = installOverlayDevtools(stubManager(), stubWindow());
  const body = await leoAds.ads();
  assert.deepEqual(body.ads.map((a) => a.id), ['a']);
});
