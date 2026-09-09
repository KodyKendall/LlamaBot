// The "out of messages" card must not tell a paying customer they are out of
// FREE messages, and must not tell a spend-capped account to count messages.
//
// Background: until 2026-09-05 only free accounts could ever be blocked, so the
// card hardcoded "your free messages". Per-plan daily caps landed and a Pro
// annual customer (leo-rofme) hit that sentence, then opened the pricing page
// three times trying to buy a plan he already had. A second block — a per-account
// daily spend ceiling — reached the same card with an even worse sentence.
//
// The mothership now sends block_reason / plan / resets_at. These tests pin the
// two rules that customer's experience produced: never say "free" unless the
// plan IS free, and never print a cap number (the caps are unpublished).

import assert from 'node:assert/strict';
import test from 'node:test';

import {
  EFFICIENCY_WIKI_URL,
  localClockTime,
  paywallCardCopy,
  resetPhrase,
} from '../../frontend/chat/messages/paywallCopy.js';

const AT_MIDNIGHT = '2026-09-06T00:00:00-04:00';
// 20:00 in their -04:00 day, i.e. four hours before the reset above.
const FOUR_HOURS_BEFORE = new Date('2026-09-05T20:00:00-04:00');

// ---------------------------------------------------------------------------
// Never say "free" to someone who pays
// ---------------------------------------------------------------------------

for (const plan of ['starter', 'pro', 'business']) {
  test(`a ${plan} customer is never told their FREE messages ran out`, () => {
    const { title, subtitle } = paywallCardCopy(
      { block_reason: 'message_limit', plan, resets_at: AT_MIDNIGHT },
      FOUR_HOURS_BEFORE,
    );

    assert.ok(!/free/i.test(title), `title says "free" to a ${plan} customer: ${title}`);
    assert.ok(!/free/i.test(subtitle), `subtitle says "free" to a ${plan} customer: ${subtitle}`);
    assert.match(title, /today's messages/);
  });
}

test('a free account still gets the free-account sentence', () => {
  const { title } = paywallCardCopy(
    { block_reason: 'message_limit', plan: 'free', resets_at: AT_MIDNIGHT },
    FOUR_HOURS_BEFORE,
  );
  assert.match(title, /free messages/);
});

test('the paying-customer card points at starting a new chat', () => {
  // The one lever the customer has that we can actually recommend.
  const { subtitle } = paywallCardCopy({ block_reason: 'message_limit', plan: 'pro' });
  assert.match(subtitle, /new chat/);
});

// ---------------------------------------------------------------------------
// A spend ceiling is not a message count
// ---------------------------------------------------------------------------

test('a spend-limit block does not talk about running out of messages', () => {
  const { title, subtitle } = paywallCardCopy(
    { block_reason: 'spend_limit', plan: 'business', resets_at: AT_MIDNIGHT },
    FOUR_HOURS_BEFORE,
  );

  assert.ok(!/free/i.test(subtitle), subtitle);
  assert.ok(!/used your/i.test(title), title);
  assert.match(title, /usage limit/);
  assert.match(subtitle, /new chat/);
});

// ---------------------------------------------------------------------------
// "Come back tomorrow" was a lie for most of the fleet; render the real time
// ---------------------------------------------------------------------------

test("the customer's own clock time is read out of the offset, not the browser's", () => {
  // The browser here is deliberately nowhere near -04:00.
  assert.equal(localClockTime('2026-09-06T00:00:00-04:00'), '12:00 AM');
  assert.equal(localClockTime('2026-09-06T05:30:00+09:00'), '5:30 AM');
  assert.equal(localClockTime('2026-09-06T13:00:00+00:00'), '1:00 PM');
  assert.equal(localClockTime('2026-09-06T12:15:00-07:00'), '12:15 PM');
});

test('a reset four hours out names the clock time', () => {
  assert.equal(resetPhrase(AT_MIDNIGHT, FOUR_HOURS_BEFORE), 'at 12:00 AM');
});

test('a reset two hours out says so — it converts differently', () => {
  const twoHoursBefore = new Date('2026-09-05T22:00:00-04:00');
  assert.equal(resetPhrase(AT_MIDNIGHT, twoHoursBefore), 'in about 2 hours');
});

test('a reset inside the hour says "in under an hour"', () => {
  const soon = new Date('2026-09-05T23:20:00-04:00');
  assert.equal(resetPhrase(AT_MIDNIGHT, soon), 'in under an hour');
});

test('a reset already in the past falls back to the clock time', () => {
  // A stale block must not render "in about -3 hours".
  const after = new Date('2026-09-06T03:00:00-04:00');
  assert.equal(resetPhrase(AT_MIDNIGHT, after), 'at 12:00 AM');
});

test('the real reset time reaches the card', () => {
  const { subtitle } = paywallCardCopy(
    { block_reason: 'message_limit', plan: 'pro', resets_at: AT_MIDNIGHT },
    FOUR_HOURS_BEFORE,
  );
  assert.match(subtitle, /reset at 12:00 AM/);
});

// ---------------------------------------------------------------------------
// Rollout: an older mothership sends none of these fields
// ---------------------------------------------------------------------------

test('an empty frame reproduces the pre-0.7.7 card', () => {
  const { title, subtitle } = paywallCardCopy({});
  assert.match(title, /free messages/);
  assert.match(subtitle, /tomorrow/);
});

test('a missing resets_at falls back to the word tomorrow', () => {
  assert.equal(resetPhrase(undefined), null);
  assert.equal(resetPhrase('not a timestamp'), null);
  const { subtitle } = paywallCardCopy({ block_reason: 'spend_limit', plan: 'pro' });
  assert.match(subtitle, /resets tomorrow/);
});

test('an unrecognised block_reason is treated as a message limit', () => {
  // Forwards compatibility: a reason we have never heard of must still render a
  // sensible card rather than an empty one.
  const { title } = paywallCardCopy({ block_reason: 'something_new', plan: 'free' });
  assert.match(title, /free messages/);
});

// ---------------------------------------------------------------------------
// Never print a cap number — the per-plan limits are not published
// ---------------------------------------------------------------------------

test('no card prints a message count', () => {
  for (const plan of ['free', 'starter', 'pro', 'business']) {
    for (const reason of ['message_limit', 'spend_limit']) {
      const { title, subtitle } = paywallCardCopy(
        { block_reason: reason, plan, resets_at: AT_MIDNIGHT, messages_remaining: 0 },
        FOUR_HOURS_BEFORE,
      );
      assert.ok(!/\b(30|60|120|250)\b/.test(title + subtitle),
        `a cap number leaked into the ${plan}/${reason} card`);
    }
  }
});

test('the efficiency wiki page is the one the mothership published', () => {
  assert.equal(EFFICIENCY_WIKI_URL,
    'https://llamapress.ai/wiki/using-your-messages-efficiently');
});
