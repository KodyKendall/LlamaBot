# Instance sleep lock

The "Your free Leo is about to sleep" modal. The mothership decides that a free
instance is going to sleep; the instance blocks new turns and shows a
non-dismissible upgrade prompt until the mothership says otherwise.

## Contract (what the mothership calls)

```
POST https://<instance>.llamapress.ai/api/instance-lock
Authorization: Bearer <the instance's mothership_api_token>
Content-Type: application/json

{"locked": true}
```

Optional overrides — change the copy without shipping a new image:

```json
{
  "locked": true,
  "title": "Your free Leo is about to sleep",
  "body": "We are backing up your Leo so you don't lose your work. Upgrade for a Leo that never sleeps.",
  "upgrade_url": "https://llamapress.ai/pricing"
}
```

Unlock (e.g. the user upgraded) is the same call with `{"locked": false}`. The
response is the normalized state. `401` means the bearer token didn't match the
`mothership_api_token` in that instance's `.leonardo/instance.json`; `400` means
the overridden copy doesn't fit in `SiteSetting.value` (1000 chars of JSON).

**Auth is deliberately the mothership token, not admin.** The instance owner is
an admin on their own box, so an admin-gated write would let them curl their own
lock off. For the same reason `instance_lock` is not in `VALID_SITE_SETTINGS`.

Backstop: if `POST /api/leonardo/lease_renew` returns an `instance_lock` object,
`LeaseManager` applies it on the next check (≤5 min). That covers the case where
the mothership can't reach the instance inbound. It is a no-op if the key is
absent, so an older mothership never triggers it.

## How the browser finds out (no refresh)

Three paths, cheapest first:

1. **Server injection** — `ui.py` writes `window.LLAMABOT_INSTANCE_LOCK` into the
   page, so a locked instance paints the modal on the first frame instead of
   flashing a usable UI.
2. **Poll** — `chat.html` polls `GET /api/instance-lock` every 10s. This is what
   catches a lock (or an unlock) that lands while the tab is already open.
3. **WebSocket frame** — a locked instance answers a submitted message with
   `{"type": "instance_locked", ...}`, which locks the UI instantly rather than
   waiting out the poll.

A failed poll keeps the last known state — losing the network must not unlock the
instance, and must not lock a healthy one.

## Enforcement

`RequestHandler._check_instance_lock_or_block` refuses turns before the paywall
gate (a blocked turn must not burn quota). Hiding the overlay in devtools buys
nothing. The check reads the auth DB per user message and **fails open**: a down
database must never lock a paying user out of their own Leo.

## Storage

One `SiteSetting` row, `instance_lock`, holding the JSON payload — durable across
container restarts and `bin/update`, unlike an env var (which also can't be
flipped without a `--force-recreate`).

## Tests

- `app/tests/test_instance_lock.py` — state round-trip, fail-open, mothership-only
  auth, wiring.
- `app/tests/js/instance_lock_modal.test.mjs` — the real chat.html IIFE against a
  DOM stub: first paint without a fetch, mid-session lock/unlock, Escape is
  swallowed, poll failure keeps state.
