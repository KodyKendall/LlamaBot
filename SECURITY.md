# Security Policy

## Reporting a Vulnerability

Please report security issues privately. **Do not open a public GitHub issue** for
a suspected vulnerability.

Two private channels, either is fine:

1. **GitHub Private Vulnerability Reporting** — the "Report a vulnerability" button
   under this repository's [Security tab](https://github.com/KodyKendall/LlamaBot/security).
   Preferred, since it keeps the report, the discussion, and the eventual advisory
   in one place.
2. **Email** — [kody@llamapress.ai](mailto:kody@llamapress.ai).

Useful things to include, to the extent you have them:

- The affected file(s) and function(s), and the commit SHA you tested against.
- What you actually reproduced, kept separate from what you inferred from reading
  the code. Both are welcome; knowing which is which speeds up triage a lot.
- A reproduction as plain-text source (not a binary or an opaque script), plus
  requests/responses with secrets redacted.
- Any modifications you made to your test environment.

## What to Expect

- **Acknowledgement** within 3 business days.
- **Initial assessment** — whether we can reproduce it, and our read on severity —
  within 7 days.
- **Progress updates** at least every 7 days while a fix is in flight.
- **Credit** in the release notes and advisory, unless you'd rather stay anonymous.

We ask that you give us a reasonable window to ship a fix before public discussion.
We aren't going to argue about a specific number of days up front — tell us the
timeline you're working to and we'll tell you honestly whether we can meet it.

LlamaBot is a small project. We'd rather hear about a problem early and imperfectly
than not at all.

## Scope

This repository is the LlamaBot FastAPI + LangGraph runtime. Related repositories
(`LlamaPress-Simple`, `llama_bot_rails`) are in scope for reports sent here as well;
we'll route them.

**Especially interesting to us**, given what this project is:

- Anywhere an LLM-chosen value reaches a sensitive sink — a URL the server fetches,
  a shell command, a file path, a SQL query.
- Prompt injection that crosses a real security boundary, i.e. untrusted content
  the agent reads causing an action the operator did not authorize. (Not: "the model
  can be talked into saying something wrong.")
- Authentication and authorization gaps in the FastAPI routes or the Rails bridge.
- Secrets reaching logs, checkpoints, telemetry, or the model's context.
- Sandbox and container escapes from agent tool execution.

**Out of scope:**

- Findings that require the operator to already be running an intentionally
  dangerous configuration that we document as dangerous.
- The agent modifying its own instance's files. That is the product; agents run
  with intended write access to the app they are building.
- Automated scanner output with no demonstrated impact.
- Denial of service through resource exhaustion by an already-authenticated
  operator on their own instance.

## Notes on Agent Tooling

LlamaBot runs LLM agents with real tools. Two properties we try to hold, and would
like to hear about when we've failed to:

- **A tool that makes the server open a URL is an SSRF surface** whenever the model
  can be influenced by content it reads. Server-side navigation must go through
  `app/agents/utils/url_guard.py`, which permits the instance's own Rails app
  and public addresses and nothing else.
- **Powerful tools are gated by an operator setting and fail closed** when that
  setting can't be read (see `enable_browser_inspect`, `enable_live_browser_tools`).

## Past Reports

- **2026-08** — SSRF in the experimental page-clone tool, reported by Sanjay
  Krishnegowda with Shenao Wang and Xinyi Hou. An LLM-supplied URL reached
  Playwright's `page.goto()` with no destination validation. The feature was
  unused and experimental, so it was removed rather than patched; the remaining
  server-side navigation tool was put behind the URL guard described above.
