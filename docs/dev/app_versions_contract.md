# Saved whole-app versions (LP-47) — box ↔ mothership contract

The box half shipped in LlamaBot 0.7.21. The mothership half (snapshots, S3, restore,
new-box launch, plan limits, audit log) is Mother Leo's. This is the shape the box
sends and expects. Anything not listed here is dropped by the box before it reaches
the browser.

## What the box does

The browser calls the box's own `/api/versions` routes (signed in, same origin). The box
forwards each one to the mothership with its box token and the signed-in user, and passes
the status code and an allowlisted body straight back.

| Browser → box | Box → mothership |
| --- | --- |
| `GET /api/versions` | `GET /api/leonardo/versions?instance_name=<box>` |
| `POST /api/versions` `{name}` | `POST /api/leonardo/versions` |
| `DELETE /api/versions/:id` | `DELETE /api/leonardo/versions/:id` |
| `POST /api/versions/:id/restore` `{confirm}` | `POST /api/leonardo/versions/:id/restore` |
| `POST /api/versions/:id/launch` | `POST /api/leonardo/versions/:id/launch` |

Every mothership call carries `Authorization: Bearer <mothership_api_token>`. Every
non-GET body is:

```json
{ "instance_name": "<box>", "user": { "id": 7, "username": "ana", "email": "…",
  "llamapress_user_guid": "…", "role": "…", "is_admin": false }, "…": "…" }
```

plus `name` (save, trimmed, ≤ 80 chars) or `confirm` (restore: the version name the
user typed). The box refuses an empty name/confirm and any id outside
`[A-Za-z0-9_-]{1,64}` without calling the mothership. Authorize on the mothership with
`UserInstance#viewable_by?` against `user.llamapress_user_guid`; legacy users have no
guid (refuse them or fall back to box-owner rules — mothership's call).

The GET has no body, so it carries no user. Listing is gated by the box's sign-in only.

## What the mothership returns

`GET` (and the list after any change):

```json
{
  "enabled": true,
  "message": "optional text shown verbatim",
  "upgrade_url": "https://llamapress.ai/pricing",
  "can_restore": true,
  "can_launch": true,
  "limit": { "used": 1, "max": 1 },
  "versions": [
    { "id": "v_1", "name": "before invoices", "created_at": "2026-11-10T14:00:00Z",
      "size_bytes": 52428800, "status": "saving|saved|failed", "kind": "user|safety" }
  ],
  "restore": { "status": "idle|restoring", "version_id": "v_1" }
}
```

- `enabled: false` with no `message` hides the whole Versions section (the rollback switch).
  With a `message`, the section shows it and offers no buttons.
- `can_restore: false` hides Restore (e.g. Free plan); same for `can_launch`.
- `status: "saving"` makes the browser re-list every 5s until it turns `saved`/`failed`.
- `restore.status: "restoring"` is how the browser knows a restore is underway and
  when it has finished. **Please send it**: without it the browser falls back to
  "the box went down and came back", which can fire early if the box never restarts.

Save → `201 {"version": {…}}`. Restore → `202 {"restore": {…}, "safety_version": {…}}`.
Launch → `202 {"instance": {"name", "status", "url", "dashboard_url"}}`. Delete → `200 {"deleted": true}`.

Errors: any 4xx/5xx with `{"error": "<plain sentence>", "upgrade_url": "…"}`. The
browser shows `error` verbatim and `upgrade_url` as a "See plans and pricing" link.
Use `402` for the plan limit.

## What never gets through (LP-45)

The box keeps only these fields: top level `enabled, message, error, upgrade_url,
deleted, can_restore, can_launch`; version `id, name, created_at, size_bytes, status,
kind, note`; `restore` `status, version_id, started_at, message`; `instance` `name,
status, url, dashboard_url`; `limit` `used, max`. Nested objects outside those are dropped.

Links (`upgrade_url`, `url`, `dashboard_url`) survive only if they are `https`, on the
mothership's host or a subdomain of it, have no query string, and have no
download/export/archive/`.tar`/`.zip`/`.sql`/`.dump` in the path. A pre-signed S3 URL
can't pass any of those, and there is no box route that streams a version.
