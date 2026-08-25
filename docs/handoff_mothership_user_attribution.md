# Mothership handoff — who sent the message (user attribution on telemetry)

**From:** LlamaBot 0.7.4 (shipping now)
**To:** LlamaPress.ai mothership
**Instance-side code:** `app/services/user_context.py`

## The gap

Every report an instance sends — `report_message`, `report_error`,
`report_turn_metrics`, `submit_feedback` — carried `instance_name` and nothing
about *who* was at the keyboard. A box with three people on it produced one
undifferentiated stream: "instance foo sent 400 messages" with no way to say
which account sent which, which user is hitting the errors, or whose turns are
slow.

## What instances now send

All four report endpoints may now carry an optional `user` object:

```json
"user": {
  "id": 3,
  "username": "kody@llamapress.ai",
  "email": "kody@llamapress.ai",
  "llamapress_user_guid": "b7f0…",
  "role": "engineer",
  "is_admin": true
}
```

The same shape already goes to `POST /api/leonardo/overlay_ads` (it previously
sent a four-field subset; it now sends this).

**`llamapress_user_guid` is the join key, and it is yours.** The mothership
minted it during unified login and the instance stored it on the local shadow
user (`app/routers/unified_login.py`). The box is not translating one identity
into another — it is echoing back a foreign key you already own. So the mapping
to a llamapress.ai account belongs on your side: `guid -> user`, one lookup.

**Do not join on `email`.** It is a synced copy of the mothership profile,
carried for human readability in triage only. Matching accounts by email is
precisely the spoofable path `unified_login` refuses to take; the same reasoning
applies to the reporting side.

## Cases where identity is partial or absent

The field is **omitted entirely** when the instance does not know who is acting.
Treat a missing `user` as "unattributed", never as "the box's only user".

| Case | What arrives |
|---|---|
| Unified-login (SSO) user | Full object, `llamapress_user_guid` set — maps to an account |
| Legacy username/password user | Object with `llamapress_user_guid: null` — identifiable per-box only, because no llamapress.ai account exists to map to |
| `llama_bot_rails` gem token (Rails-embedded chat) | Partial object: `{username: "rails_user:5", rails_user_id: 5, source: "llama_bot_rails", id: null, llamapress_user_guid: null}` — a **Rails** user id, a different namespace from yours; never resolve it against llamapress.ai accounts |
| Unauthenticated / DB unreachable / pre-0.7.4 instance | No `user` key at all |

## Asks

1. Persist `user` on `InstanceMessage`, `InstanceError`, the turn-metrics row and
   the feedback annotation. A jsonb column plus a resolved `user_id` FK
   (populated from `llamapress_user_guid` when it matches) is enough.
2. Segment the existing dashboards by resolved user, keeping "unattributed" as a
   visible bucket rather than folding it into the box owner.
3. `overlay_ads` can now target on `llamapress_user_guid` instead of the box-local
   `id`, which is stable across instances for the same person.

Backward compatible in both directions: instances that don't send `user` behave
exactly as before, and a mothership that ignores the field loses nothing.
