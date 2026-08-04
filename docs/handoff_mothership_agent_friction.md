# Mothership Handoff: agent friction reports (`source: "agent_friction"`)

## Overview

Leos can now file **friction reports** — structured complaints about their own tooling.
A tool erroring in a way its description never warned about, a root-owned file every edit
silently fails on, output that contradicts the docs, a capability that isn't there and
gets routed around: none of that raises an exception, so until now it died in the
transcript and we only heard about it secondhand, from a customer complaining about the
downstream symptom.

**The LlamaBot side is complete and shipping in 0.6.0h.** It deliberately reuses the
existing error pipeline rather than adding an endpoint: `report_friction` →
`MothershipClient.report_error` → `POST /api/leonardo/report_error`. So there is **no new
endpoint to build**.

The mothership needs three things:

1. **Add `agent_friction` to the `source` allowlist** (one-line change — without it these
   rows are silently mislabeled `llamabot`).
2. **Keep friction out of the exception stream in the dashboard** — a filter or its own
   view.
3. **Provision an instance token for `llamapress-dev`** so the dev box can exercise this
   live. Details and the caveat in the last section.

---

## 1. The `source` allowlist (the blocking one)

`POST /api/leonardo/report_error` currently allowlists `source` to
`%w[llamabot rails_app frontend]` and silently defaults anything else to `"llamabot"`.
Friction reports arrive today and land in the queue **wearing the wrong source label**.

```ruby
# wherever the allowlist lives on the receiver
ALLOWED_SOURCES = %w[llamabot rails_app frontend agent_friction].freeze
```

Nothing else about the endpoint changes. Until this ships, friction is still greppable —
`error_class` always starts with `AgentFriction.` — but please don't build the dashboard
filter on that string; use `source` once it's allowlisted.

---

## 2. The exact payload

This is a **real captured POST** from the shipping code path (LlamaBot 0.6.0h, captured
2026-08-04 against a local stub), not a hand-written example:

```json
{
  "instance_name": "llamapress-dev",
  "source": "agent_friction",
  "error_class": "AgentFriction.permissions",
  "error_message": "edit_file refused to write app/models/application_record.rb with Permission denied. The file is owned by root, so nothing I do from the agent can change it and I could not finish the requested edit.",
  "traceback": "severity: blocked\ncategory: permissions\ntool: edit_file\nagent_mode: rails_agent\nmodel: deepseek-v4-flash\n\n--- evidence ---\nPermissionError: [Errno 13] Permission denied: '/app/rails/app/models/application_record.rb'\n\n--- suggested fix ---\nedit_file should detect EACCES and suggest fix_permissions instead of raising.",
  "fingerprint": "c96af91cce49e26d8b3c19e2e334915f",
  "thread_id": "thread-qa-probe-1",
  "agent_mode": "rails_agent",
  "model": "deepseek-v4-flash",
  "llamabot_version": "0.6.0f",
  "occurred_at": "2026-08-04T18:19:38.713057+00:00",
  "recovered": false
}
```

Auth is the same Bearer token as every other `/api/leonardo/*` call.

### How the existing fields are reused

| Field | For a crash | For friction |
|---|---|---|
| `source` | `llamabot` / `rails_app` / `frontend` | **`agent_friction`** |
| `error_class` | the Python exception class (`TypeError`) | `AgentFriction.<category>` |
| `error_message` | `str(exc)` | the agent's own 1–3 sentence description |
| `traceback` | a real stack trace | a structured block: severity, category, tool, agent_mode, model, verbatim `evidence`, `suggested_fix` |
| `recovered` | did the graceful floor still answer the user | **did the agent get past it** — `false` only when severity is `blocked` |
| `fingerprint` | md5 of `error_class\|first line\|agent_mode` | md5 of `category\|tool_name\|first line` |

`category` is one of: `tool_error`, `permissions`, `confusing_output`,
`missing_capability`, `environment`, `docs_mismatch`, `slow`, `other`.
`severity` is one of: `blocked`, `workaround`, `annoyance`.

Both are the agent's judgement call and are already coerced to the allowed set on the
instance side, so the receiver can trust them — but they arrive embedded in
`error_class` / `traceback`, not as their own columns. **If you want to facet on them,
parsing `error_class` after the dot is reliable; parsing `traceback` is not.** Promoting
`category` and `severity` to real columns would be a nice-to-have, not a blocker.

---

## 3. Dashboard treatment (please don't merge these into the error list)

Friction rows are **self-reported and subjective**, with no stack trace and no guarantee
the complaint is even correct — the agent might be wrong about why something failed.
Mixed into the exception stream they wreck triage in both directions: they bury real
crashes, and they get dismissed as noise.

What they want:

- **Their own view or filter**, keyed on `source = "agent_friction"`.
- **Sorted by rollup `count`, not recency.** The existing `InstanceError` fingerprint
  rollup is exactly right here: a papercut that 40 instances independently reported is a
  roadmap item, and one box complaining once is not. This is the single most valuable
  thing the dashboard can do with this data.
- **`suggested_fix` surfaced**, not buried in the traceback blob. Agents frequently hand
  over the actual fix; it's the highest-value line in the report.
- Faceting by `agent_mode` and `model` — friction concentrated in one mode usually means
  a prompt or toolset problem in that mode specifically.

### Volume expectations

Deliberately low. The instance side dedupes by fingerprint and **caps at 3 reports per
conversation** (delegated sub-agents share the parent's budget, they don't get their own).
A retry loop cannot produce a flood. If you see a single instance filing hundreds of
these, that's a bug on our side — tell us.

---

## 4. Provision an instance token for `llamapress-dev`

The dev box (Hetzner `llamapress-dev`, **UserInstance #1146** — the pinned one) has
`instance.json` with `instance_name: CHANGEME_INSTANCE_NAME` and an empty
`mothership_api_token`, so `MothershipClient.enabled` is `false` and **every** phone-home
is dropped before it hits the network. That's why none of this telemetry has ever been
verifiable end-to-end from the box where it's developed.

**The ask:** issue #1146 its `mothership_api_token` and `instance_name` so the box can
report for real.

Two conditions on that, both important:

1. **Flag #1146 as internal/dev, and exclude it from the customer-facing dashboards and
   from any alerting.** It's the box where we deliberately break things. Every experiment,
   every half-finished branch, every intentionally-triggered exception would otherwise
   land in the same queue you read customer signal out of. A `dev`/`internal` boolean on
   the UserInstance, defaulting the error views to `WHERE NOT internal`, is enough.
2. **Scope it to that instance only.** A token that can only report as #1146 is fine to
   put on the dev box. Anything broader is not — per `~/dev/CLAUDE.md`, production
   credentials don't live on that machine, and Darren needs to sign off on this either way.

If #1 is a problem, say so and we'll keep the box on the local stub instead
(`scripts/mothership_stub.py` in LlamaBot) — that already verifies our side of the wire.
The only thing the stub genuinely can't test is **your** receiver, which is exactly the
part this handoff is about.

---

## References

- Tool + delivery: `LlamaBot/app/agents/leonardo/friction.py`
- Transport (unchanged): `LlamaBot/app/services/mothership_client.py` → `report_error`
- Design notes: `LlamaBot/docs/dev/error_telemetry.md` §4b
- Automated coverage: `LlamaBot/app/tests/test_report_friction.py`
- Manual QA checklist: `LlamaBot/docs/test_plans/0.6.0h.md`
- Local fake mothership for reproducing the POST: `LlamaBot/scripts/mothership_stub.py`
