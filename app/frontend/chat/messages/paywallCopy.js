/**
 * Copy for the "out of messages" card.
 *
 * Until 2026-09-05 the mothership treated anyone with a subscription as always
 * allowed, so only free users could ever see this card and it safely hardcoded
 * "You've used your free messages for today". That is no longer true: every plan
 * now has a real per-plan daily cap, and a second, different block exists — a
 * per-account daily SPEND ceiling, for which a message count is the wrong story
 * entirely.
 *
 * A Pro annual customer hit the old card on 2026-09-05, was told he was out of
 * *free* messages, and opened the pricing page three times trying to work out
 * what to buy a plan he already pays for. Hence the two hard rules here:
 *
 *   1. Never say "free" unless the mothership says the plan is free.
 *   2. Never print a cap number — the per-plan limits are deliberately not
 *      published, and the box is not told them.
 *
 * Everything comes off the paywall frame; nothing is inferred locally. In
 * particular the daily window resets on the customer's OWN local day, which only
 * the mothership can resolve from their geolocated IP — so `resets_at` arrives
 * as ISO 8601 carrying their own UTC offset, and we render the clock time out of
 * the string rather than out of the browser's timezone.
 *
 * An older mothership sends none of these fields. The fallbacks below are
 * exactly today's behaviour, so a box on a new image against an old mothership
 * reads the same as it always did.
 */

/** Where the "use your messages efficiently" wiki page lives. */
export const EFFICIENCY_WIKI_URL = 'https://llamapress.ai/wiki/using-your-messages-efficiently';

/** Hours inside which "resets in about 2 hours" beats naming a clock time. */
const SOON_HOURS = 3;

const ISO_WITH_OFFSET = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/;

/**
 * The customer's local clock time for `resetsAt`, e.g. "12:00 AM".
 *
 * Read out of the timestamp's own fields rather than via toLocaleTimeString, so
 * the time shown is the start of THEIR day even when the browser is somewhere
 * else (a shared box, a VPN, a founder looking at a customer's app).
 *
 * @returns {string|null} null when the value is missing or unparseable.
 */
export function localClockTime(resetsAt) {
  const match = ISO_WITH_OFFSET.exec(String(resetsAt || ''));
  if (!match) return null;

  const hour24 = Number(match[4]);
  const minute = match[5];
  if (!Number.isFinite(hour24) || hour24 > 23) return null;

  const suffix = hour24 < 12 ? 'AM' : 'PM';
  const hour12 = hour24 % 12 === 0 ? 12 : hour24 % 12;
  return `${hour12}:${minute} ${suffix}`;
}

/**
 * "at 12:00 AM" / "in about 2 hours" / "in under an hour", or null.
 *
 * "resets in about 2 hours" converts very differently from "come back tomorrow",
 * so a reset that is nearly here says so instead of naming a time.
 *
 * @param {string} resetsAt ISO 8601 with the customer's own UTC offset.
 * @param {Date} [now] injectable for tests.
 */
export function resetPhrase(resetsAt, now = new Date()) {
  const clock = localClockTime(resetsAt);
  if (!clock) return null;

  const at = new Date(resetsAt);
  const hoursAway = (at.getTime() - now.getTime()) / 3600000;

  // A reset in the past means the window has already rolled over and the block
  // is stale — naming a time that has been and gone reads as broken, so fall
  // back to the clock time rather than "in -1 hours".
  if (Number.isFinite(hoursAway) && hoursAway > 0 && hoursAway < SOON_HOURS) {
    if (hoursAway < 1) return 'in under an hour';
    const rounded = Math.round(hoursAway);
    return `in about ${rounded} ${rounded === 1 ? 'hour' : 'hours'}`;
  }

  return `at ${clock}`;
}

/**
 * Title and subtitle for the card.
 *
 * @param {object} detail the paywall_hit frame: {block_reason, plan, resets_at}.
 * @param {Date} [now] injectable for tests.
 * @returns {{title: string, subtitle: string}}
 */
export function paywallCardCopy(detail = {}, now = new Date()) {
  // Missing values are the old mothership's shape. A box must not guess a plan
  // it was not told about, and "free" is the safe read only because it is what
  // this card said to everyone before per-plan caps existed.
  const blockReason = detail.block_reason || 'message_limit';
  const plan = detail.plan || 'free';
  const phrase = resetPhrase(detail.resets_at, now);

  if (blockReason === 'spend_limit') {
    // Not a message count, so no message-count sentence and no "you've used".
    // The lever the customer actually has here is chat size, not message count.
    return {
      title: "This app has hit today's usage limit",
      subtitle: `It resets ${phrase || 'tomorrow'}. Larger apps use more with each `
        + 'message — starting a new chat for a new topic helps.',
    };
  }

  const resetSentence = phrase
    ? `Your messages reset ${phrase}.`
    : 'Your messages come back tomorrow.';

  if (plan === 'free') {
    return {
      title: "You've used your free messages for today",
      subtitle: `${resetSentence} Upgrade for more messages each day.`,
    };
  }

  return {
    title: "You've used today's messages for this app",
    subtitle: `${resetSentence} Upgrade for more each day, or start a new chat `
      + 'to make them go further.',
  };
}
