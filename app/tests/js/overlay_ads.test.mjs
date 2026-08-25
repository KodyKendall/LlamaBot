// Building-overlay promo slot ("jumbotron").
//
// The snippets are remote HTML rendered inside the signed-in chat page, so the
// property that actually matters is the sandbox: `allow-scripts` WITHOUT
// `allow-same-origin`, which keeps a promo in an opaque origin where it can
// animate itself but can't touch the session. The rest is rotation mechanics and
// the fail-open contract (any fetch trouble → no slot at all).
//
// Drives the real module against a stub DOM, like building_overlay_escape.

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  AD_SANDBOX,
  DEFAULT_ROTATE_SECONDS,
  OverlayAdRotator,
  adDocument,
  loadOverlayAds,
} from '../../frontend/chat/ui/OverlayAds.js';

class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.attrs = {};
    this.style = {};
    this.children = [];
    this._html = '';
  }
  setAttribute(k, v) { this.attrs[k] = v; }
  getAttribute(k) { return this.attrs[k]; }
  appendChild(c) { this.children.push(c); return c; }
  set innerHTML(v) { this._html = v; if (v === '') this.children = []; }
  get innerHTML() { return this._html; }
}

const fakeDoc = { createElement: (tag) => new FakeElement(tag) };

/** setInterval/setTimeout capture, so rotation is testable without waiting. */
function fakeTimers() {
  const t = {
    intervals: [],
    timeouts: [],
    setInterval(fn, ms) { t.intervals.push({ fn, ms }); return t.intervals.length; },
    clearInterval(id) { t.intervals[id - 1] = null; },
    setTimeout(fn, ms) { t.timeouts.push({ fn, ms }); return t.timeouts.length; },
    clearTimeout(id) { t.timeouts[id - 1] = null; },
    // Fire the rotation tick and then its cross-fade callback.
    tick() {
      t.intervals.filter(Boolean).forEach((i) => i.fn());
      const pending = t.timeouts.filter(Boolean);
      t.timeouts = [];
      pending.forEach((x) => x.fn());
    },
  };
  return t;
}

const ADS = [
  { id: 'one', html: '<b>One</b>', height: 140 },
  { id: 'two', html: '<b>Two</b>', height: 200 },
  { id: 'three', html: '<b>Three</b>', height: 140 },
];

function mount(ads, rotateSeconds = 180) {
  const timers = fakeTimers();
  const slot = new FakeElement('div');
  const seen = [];
  const rotator = new OverlayAdRotator({
    ads, rotateSeconds, doc: fakeDoc, timers, onAdChange: (ad) => seen.push(ad.id),
  });
  const mounted = rotator.mount(slot);
  return { rotator, slot, timers, mounted, seen, frame: rotator.frame };
}

// -- the sandbox ------------------------------------------------------------

test('ad frame is sandboxed with scripts but NOT same-origin', () => {
  const { frame } = mount(ADS);
  const flags = frame.getAttribute('sandbox').split(/\s+/);

  assert.ok(flags.includes('allow-scripts'), 'promos may animate themselves');
  assert.ok(
    !flags.includes('allow-same-origin'),
    'allow-same-origin + allow-scripts cancels the sandbox and hands the promo the session',
  );
  assert.ok(flags.includes('allow-popups'), 'a promo link must be able to open a tab');
  assert.equal(AD_SANDBOX, frame.getAttribute('sandbox'));
});

test('ad frame does not leak the instance URL as a referrer', () => {
  const { frame } = mount(ADS);
  assert.equal(frame.getAttribute('referrerpolicy'), 'no-referrer');
});

test('snippet is wrapped so links open in a new tab', () => {
  const doc = adDocument('<a href="https://llamapress.ai">Go</a>');
  assert.match(doc, /<base target="_blank"/);
  assert.match(doc, /<a href="https:\/\/llamapress\.ai">Go<\/a>/);
});

// -- rotation ---------------------------------------------------------------

test('first ad renders immediately', () => {
  const { frame, seen } = mount(ADS);
  assert.match(frame.srcdoc, /<b>One<\/b>/);
  assert.deepEqual(seen, ['one'], 'the overlay is told which promo is up');
});

test('the rotator does NOT size its own slot', () => {
  // How much room a promo gets is the overlay's call — it fills the pane while
  // Leo starts up, but yields to the todo list once there's a plan. A rotator
  // that wrote slot.style.height would fight that on every rotation.
  const { slot } = mount(ADS);
  assert.equal(slot.style.height, undefined);
});

test('rotation advances on the mothership cadence and wraps around', () => {
  const { frame, timers, rotator, seen } = mount(ADS, 180);

  assert.equal(timers.intervals[0].ms, 180 * 1000);

  timers.tick();
  assert.match(frame.srcdoc, /<b>Two<\/b>/);
  assert.equal(rotator.currentAd.height, 200, 'the promo carries its own height hint');

  timers.tick();
  assert.match(frame.srcdoc, /<b>Three<\/b>/);

  timers.tick();
  assert.match(frame.srcdoc, /<b>One<\/b>/, 'wraps back to the first promo');
  assert.equal(rotator.index, 0);
  assert.deepEqual(seen, ['one', 'two', 'three', 'one']);
});

test('a single ad is a static banner — no rotation timer', () => {
  const { timers, mounted } = mount([ADS[0]]);
  assert.equal(mounted, true);
  assert.equal(timers.intervals.length, 0);
});

test('no ads means no slot at all', () => {
  const { mounted, slot } = mount([]);
  assert.equal(mounted, false);
  assert.equal(slot.children.length, 0);
});

test('stop() kills the rotation timer and empties the slot', () => {
  const { rotator, timers, slot } = mount(ADS);
  rotator.stop();

  assert.equal(timers.intervals.filter(Boolean).length, 0);
  assert.equal(slot.children.length, 0);
  assert.equal(rotator.frame, null);

  rotator.stop(); // idempotent — removeStreamingOverlay can run twice
});

// -- fail-open fetch --------------------------------------------------------

test('loadOverlayAds returns the payload on success', async () => {
  const out = await loadOverlayAds(async () => ({
    ok: true,
    json: async () => ({ ads: ADS, rotate_seconds: 90 }),
  }));

  assert.equal(out.ads.length, 3);
  assert.equal(out.rotateSeconds, 90);
});

test('loadOverlayAds drops entries with no html', async () => {
  const out = await loadOverlayAds(async () => ({
    ok: true,
    json: async () => ({ ads: [{ id: 'blank', html: '  ' }, { id: 'ok', html: '<b>x</b>' }] }),
  }));

  assert.deepEqual(out.ads.map((a) => a.id), ['ok']);
  assert.equal(out.rotateSeconds, DEFAULT_ROTATE_SECONDS);
});

for (const [name, fetchImpl] of [
  ['a network error', async () => { throw new Error('offline'); }],
  ['a non-200', async () => ({ ok: false, json: async () => ({}) })],
  ['unparseable JSON', async () => ({ ok: true, json: async () => { throw new Error('bad'); } })],
  ['a payload with no ads key', async () => ({ ok: true, json: async () => ({}) })],
]) {
  test(`loadOverlayAds fails open on ${name}`, async () => {
    const out = await loadOverlayAds(fetchImpl);
    assert.deepEqual(out.ads, []);
    assert.equal(out.rotateSeconds, DEFAULT_ROTATE_SECONDS);
    assert.equal(out.variant, null);
    assert.equal(out.policy.enabled, true, 'a dead endpoint still yields a usable policy');
  });
}

// -- display policy: WHEN the jumbotron shows ------------------------------
//
// The product judgement is the mothership's; these pin that the client applies
// its verdict faithfully and fails open when the browser won't cooperate.

import { DEFAULT_POLICY, LAST_SHOWN_KEY, evaluatePolicy, policyAllowsMode, recordShown }
  from '../../frontend/chat/ui/OverlayAds.js';

function fakeStorage(initial = {}) {
  const map = { ...initial };
  return {
    map,
    getItem: (k) => (k in map ? map[k] : null),
    setItem: (k, v) => { map[k] = v; },
    removeItem: (k) => { delete map[k]; },
  };
}

const NOW = 1_700_000_000_000;

test('policy defaults hold back the promo for the first few seconds', () => {
  // A short build that flashes an ad and yanks it away reads as a bug.
  const v = evaluatePolicy({ ads: ADS, policy: DEFAULT_POLICY, now: NOW, storage: fakeStorage() });
  assert.equal(v.show, true);
  assert.equal(v.delayMs, DEFAULT_POLICY.show_after_seconds * 1000);
});

test('show_after_seconds: 0 shows immediately', () => {
  const v = evaluatePolicy({
    ads: ADS, policy: { ...DEFAULT_POLICY, show_after_seconds: 0 }, now: NOW, storage: fakeStorage(),
  });
  assert.equal(v.delayMs, 0);
  assert.equal(v.reason, 'ok');
});

test('enabled: false is a kill switch', () => {
  const v = evaluatePolicy({
    ads: ADS, policy: { ...DEFAULT_POLICY, enabled: false }, now: NOW, storage: fakeStorage(),
  });
  assert.equal(v.show, false);
  assert.equal(v.reason, 'disabled-by-policy');
});

test('no ads means no promo, whatever the policy says', () => {
  const v = evaluatePolicy({ ads: [], policy: DEFAULT_POLICY, now: NOW, storage: fakeStorage() });
  assert.equal(v.show, false);
  assert.equal(v.reason, 'no-ads');
});

test('cooldown suppresses a promo seen too recently, and reports how long is left', () => {
  const storage = fakeStorage({ [LAST_SHOWN_KEY]: String(NOW - 60_000) }); // 60s ago
  const v = evaluatePolicy({
    ads: ADS, policy: { ...DEFAULT_POLICY, min_interval_seconds: 1800 }, now: NOW, storage,
  });

  assert.equal(v.show, false);
  assert.match(v.reason, /^cooldown:\d+s-left$/);
});

test('cooldown lets the promo through once it has elapsed', () => {
  const storage = fakeStorage({ [LAST_SHOWN_KEY]: String(NOW - 3600_000) }); // an hour ago
  const v = evaluatePolicy({
    ads: ADS, policy: { ...DEFAULT_POLICY, min_interval_seconds: 1800 }, now: NOW, storage,
  });
  assert.equal(v.show, true);
});

test('a cooldown we cannot read fails OPEN', () => {
  // Private mode / embedded contexts throw on localStorage. Not being able to
  // read the clock is not a reason to suppress the promo.
  const hostile = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  const v = evaluatePolicy({
    ads: ADS, policy: { ...DEFAULT_POLICY, min_interval_seconds: 1800 }, now: NOW, storage: hostile,
  });

  assert.equal(v.show, true);
  assert.doesNotThrow(() => recordShown(NOW, hostile));
});

test('recordShown stamps the cooldown clock', () => {
  const storage = fakeStorage();
  recordShown(NOW, storage);
  assert.equal(storage.map[LAST_SHOWN_KEY], String(NOW));
});

test('policyAllowsMode honours a narrowed mode list', () => {
  assert.equal(policyAllowsMode({ modes: ['building'] }, 'building'), true);
  assert.equal(policyAllowsMode({ modes: ['building'] }, 'plan'), false);
  assert.equal(policyAllowsMode({ modes: [] }, 'building'), false, 'empty list = never show');
});

test('policyAllowsMode falls back to defaults when the policy is malformed', () => {
  assert.equal(policyAllowsMode(null, 'building'), true);
  assert.equal(policyAllowsMode({ modes: 'building' }, 'building'), true);
});

test('loadOverlayAds carries policy and variant through', async () => {
  const out = await loadOverlayAds(async () => ({
    ok: true,
    json: async () => ({ ads: ADS, policy: { show_after_seconds: 12 }, variant: 'arm-b' }),
  }));

  assert.equal(out.policy.show_after_seconds, 12);
  assert.equal(out.policy.enabled, true, 'unspecified fields keep their default');
  assert.equal(out.variant, 'arm-b');
});

test('loadOverlayAds falls back to the default policy when the endpoint dies', async () => {
  const out = await loadOverlayAds(async () => { throw new Error('offline'); });
  assert.deepEqual(out.policy, { ...DEFAULT_POLICY });
  assert.equal(out.variant, null);
});
