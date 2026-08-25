# SSO return-to-brand (`sso_origin`) — 0.7.4

## The bug

The mothership Rails app now answers on two domains: `llamapress.ai` (original)
and `builtwithleo.com` (new). A box only knows one of them — whatever
`.leonardo/instance.json` carries as `mothership_url`, which for every box in
the fleet today is `https://llamapress.ai`.

So a user who signed up and signed in at **builtwithleo.com**, then landed in
their box, got sent to **llamapress.ai** the next time the box needed them to
re-authorize (expired grant, used grant, the login-page SSO button). Different
domain → different Rails session → a sign-in wall on a brand they have never
used.

## Why not detect it

Two guesses were considered and rejected:

- **Host header.** Instance boxes are all on `*.llamapress.ai`; there is no
  wildcard DNS on `builtwithleo.com`. The host carries no brand signal.
- **`Referer`.** Routinely stripped on a cross-origin redirect, so it would
  silently fall back to llamapress.ai most of the time.

The origin has to be *told* to the box.

## The contract

**Mothership → box.** Whenever the mothership redirects a browser into a box, it
appends its own base URL as `sso_origin`:

```
https://<box>/auth/consume?token=<grant>&sso_origin=https://builtwithleo.com
https://<box>/login?token=<magic-link>&sso_origin=https://builtwithleo.com
```

Both entry points accept it. It is optional — omitting it keeps today's
behavior exactly (fall back to `mothership_url`).

**Box side** (`app/services/sso_origin.py`):

1. Validate against an allowlist of apex domains — `llamapress.ai`,
   `builtwithleo.com`, plus whatever host `mothership_url` points at, plus
   anything in the `SSO_ORIGIN_HOSTS` env var (comma-separated, for staging).
   A host matches when it *is* an apex or is a subdomain of one. Anything else
   is ignored and we fall back to `mothership_url`.
2. Remember it in the `leo_sso_origin` cookie (HttpOnly, SameSite=Lax, 1 year).
   It is a branding preference, not a credential, and should outlive the
   session cookie.
3. Build every **user-facing** mothership link from it:
   - the `/login` page CTA — `<origin>/sso/leo/{instance_name}`, copy reads
     "Sign in with your **Leo** account" on builtwithleo.com;
   - the `/auth/consume` recovery bounce on `grant_expired`/`grant_used`/
     `grant_not_found`;
   - the "Continue with **Leo**" link on the sign-in error page.

Precedence is: this request's `?sso_origin=` → the cookie → `mothership_url`.

**Server-to-server calls are untouched.** Every `MothershipClient` API call
(lease renewal, `verify_login_grant`, telemetry, overlay ads) still goes to
`mothership_url` — that is the credentialed channel and the API token is scoped
to it. `sso_origin` only ever changes what a *browser* is pointed at.

## Open-redirect safety

`sso_origin` arrives as an unvalidated query param, so it is treated as hostile.
Only an allowlisted host is honored, and the match is host-based, not substring
— `https://builtwithleo.com.evil.com` and `https://notbuiltwithleo.com` are both
rejected. `http://` on a public brand domain is upgraded to `https://`. Path,
query and fragment are stripped; only `scheme://host[:port]` survives. A
poisoned cookie is re-validated on every read and falls back the same way.

## Brand copy

`BRAND_DISPLAY_NAMES` in `app/services/sso_origin.py` maps apex → label:
`llamapress.ai` → "LlamaPress.ai", `builtwithleo.com` → "Leo". An unlisted host
(self-hosted mothership) renders as the bare host. Change the label there, not
in the routers.

## Still on llamapress.ai

Only SSO is brand-aware. These static links in the chat UI are still hardcoded
and would need their own pass if the rebrand goes further:
`app/frontend/chat.html` (header logo, support mailto, pricing link),
`app/frontend/chat/ui/IframeManager.js` (wiki link),
`app/routers/api.py` (`COOKBOOK_URL`).

## Tests

`app/tests/test_sso_origin.py` — allowlist/lookalike rejection, precedence,
the `/auth/consume` bounce target, cookie persistence, and the `/login` CTA
host + copy.
