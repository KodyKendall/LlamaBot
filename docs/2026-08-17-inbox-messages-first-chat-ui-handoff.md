# Handoff — Inbox goes Messages-first; chat UI side of the unread badge

**Date:** 2026-08-17
**Owner of the other half:** `llama_bot_rails` (vendored in `LlamaPress-Simple/vendor/llama_bot_rails`) — changes there are done and green.
**This doc:** what the LlamaBot chat UI (`app/frontend/chat.html` + `chat/ui/IframeManager.js`) has to do, verify, or deliberately not do.

## What changed on the Rails side (already implemented)

1. **Messages is now the first Inbox tab** (`InboxHelper::INBOX_TABS`). Because
   `/llama_bot/inbox` redirects to the first tab the visitor may open, the chat UI's
   Inbox tab now lands on **Messages** instead of Tickets. No URL changed —
   `getInboxUrl()` (`app/frontend/chat/config.js`) still points at `/llama_bot/inbox`.
2. **The Inbox tab bar itself now carries a red unread badge on its Messages tab**
   (`shared/_inbox_nav`, `data-llamabot="inbox-nav-unread-badge"`). Server-rendered on
   first paint, then kept live by the existing unread bridge.
3. **`shared/_parent_unread_bridge` now polls even when the page is NOT framed.** It
   used to bail out immediately if `window.parent === window`. It still posts the same
   `{source: 'llamabot-notifications', type: 'unread-count', unreadMessages: N}` message
   up to the parent when it IS framed — **the postMessage contract is unchanged.**
4. Redundant page chrome removed across the Inbox: the `<h1>` on each page (the tab bar
   already names it), and on Messages the "Your conversations" subtitle plus the
   Feedback/Notifications cross-links. Messages now opens with a search box + a
   "+ New Chat" button in one row, threads immediately underneath.

## What LlamaBot needs to do

**Almost certainly nothing in code.** Verified while writing this:

- `chat.html:422` already renders `<span class="tab-unread-badge hidden"
  data-llamabot="messages-unread-badge">` inside the Inbox tab, and
  `IframeManager.setUnreadMessagesCount()` already writes the **number** into it
  (capped at `99+`, hidden at zero). That is exactly the "number of unread messages on
  the tab icon" behaviour we want — it is already shipped.
- The listener (`IframeManager.initUnreadBadgeListener`) matches the message shape the
  Rails bridge still sends, so item 3 above does not break it.

So the LlamaBot-side task is a **verification pass**, not a build:

- [x] Click the Inbox tab in the chat UI and confirm it opens on **Messages** (not the
      ticket board) for both an engineer and a plain user.
      *Verified by construction 2026-08-17:* `InboxController#landing_path` takes the first
      permitted tab, and Messages is both first in `INBOX_TABS` and `ability: nil`, so no
      permission path can land anyone elsewhere. The dev box serves this today — the gem is
      bind-mounted from the submodule into `leonardo-llamapress-1`.
- [ ] Send a DM to the signed-in user from another account and confirm the count appears
      **twice**: on the chat UI's Inbox tab, and on the Messages tab of the Rails tab bar
      inside the frame. Both should clear within ~20s of reading the thread (poll interval).
      *Still human-only* — needs two real accounts. Both render paths are unit-covered
      (`app/tests/js/messages_tab_unread_badge.test.mjs` here, gem specs there).
- [x] Confirm the badge still renders on a cold load (the Inbox frame's `src` is set
      eagerly in `IframeManager`, so the bridge polls even while the user sits on the App tab).
      *Covered by* "the inbox frame loads eagerly, so its unread poll runs before the tab is
      opened".

### Result of the verification pass (2026-08-17)

No behavioural code change was needed — the doc's read of `chat.html` and `IframeManager`
was correct, and all 12 tests in `app/tests/js/messages_tab_unread_badge.test.mjs` pass.
Only two stale comments were fixed (`config.js` `getInboxUrl`, `IframeManager.js:50`), which
both still enumerated the Inbox tabs Tickets-first.

Note for whoever runs these: the `app/tests/js/*.mjs` suite needs **host node 22**, not the
container's node 18 — the tests import `.js` sources as ES modules and only node ≥22 detects
module syntax without a `package.json` `type: module`. The suite is not wired into CI.

## Open question for the human

The chat UI tab is labelled **"Inbox"** while its badge counts only direct messages. Now
that the tab lands on Messages, that is coherent — but if we ever want the badge to cover
tickets/feedback/requests too, the count has to change on the **Rails** side
(`Notification.unread_message_count_for` is deliberately DM-only so a feedback mention
does not light it up), not here. Don't widen it in the frontend.

## Do NOT do

- Do not fetch the unread count from the chat UI directly. Different origin, no Devise
  session — the iframe counts and posts up for that reason.
- Do not point the Inbox tab at `/llama_bot/conversations` to "make Messages the default".
  Tab order is owned by `InboxHelper::INBOX_TABS`, and `/llama_bot/inbox` follows it; a
  hardcoded URL would dead-end anyone whose permissions change.
