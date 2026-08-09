# SSRF and the outbound URL guard

**Status:** shipped 2026-08-06. Origin: a coordinated security disclosure.

## What happened

A researcher reported that `get_screenshot_and_html_content_using_playwright`
(the page-clone tool) passed an LLM-supplied `url` straight into Playwright's
`page.goto()` with no validation of scheme, host, or resolved IP. They pointed it
at `http://localhost:8000/api/available-models` — the instance's own API — and got
the response back into the agent's context.

The report was accurate. Two things about it were worse than reported:

1. It said the default deployment doesn't ship Playwright. It does
   (`Dockerfile`, `requirements.txt`), so the tool was live, not theoretical.
2. The same file held a second, unreported SSRF of the same class:
   `image_clone_agent` fetched an LLM-supplied `image_url` via `aiohttp` with no
   validation either. Fixing only the reported symptom would have left it behind.

## What we did

Removed the clone agent entirely (`app/agents/llamapress/clone_agent.py`,
`app/agents/utils/playwright_screenshot.py`). It was experimental and unused, so
there was nothing to preserve. `"clone"` messages now fall through to the HTML
agent. Also removed `logger.info(f"API TOKEN: ...")` from four modules, found
while reviewing the same code.

The surviving server-side navigation tool, `browser_inspect`, now goes through
`app/agents/utils/url_guard.py`.

## Why the guard is an allowlist

The obvious fix — "block loopback and RFC1918" — breaks the tool. `browser_inspect`
exists to load the box's own Rails app at `http://llamapress:3000`, which *is* a
private address. A denylist that works is a denylist that makes the tool useless.

So the rule is:

- The Rails app's own origin is allowed, from `RAILS_BASE_URL`, plus its
  `localhost` / `127.0.0.1` aliases because the agent prompts use those names
  interchangeably. Operators can add more via `BROWSER_ALLOWED_ORIGINS`.
- Everything else must resolve *entirely* to public addresses. One public A record
  does not launder a private one sitting next to it.

The allowlist is matched on `host:port`, not host. Allowing the Rails app on :3000
must not imply Postgres on :5432 of the same host — that was the whole point.

## The part a single pre-flight check doesn't cover

Validating the URL once before `goto()` misses where a redirect lands and what the
page then asks the browser to fetch. `guarded_route_handler()` is installed via
`page.route("**/*", ...)` and re-runs the same validation on every request,
aborting the ones that fail. Verdicts are cached per origin so a page with fifty
subresources doesn't trigger fifty DNS lookups.

**Residual risk, stated honestly:** name-based validation cannot fully close DNS
rebinding — a host can pass the check and re-resolve to a private address on the
actual connection. The route handler raises the cost substantially but is not a
proof. The real backstop for a genuinely hostile model is outbound network
isolation at the container level, which we do not currently have.

## The rule going forward

Any tool that makes the **server** open a URL the model chose is an SSRF surface,
because the model can be steered by content it reads. Route it through
`validate_outbound_url()`. `app/tests/test_clone_agent_removed.py` enforces this:
it fails on any `page.goto(` in the tree that isn't in a module importing the guard.

Note the distinction — `navigate_browser` / `execute_browser_js` drive the *user's*
browser tab via a frontend round-trip. Those aren't SSRF; they carry a different
risk (acting as the user), and are gated by `enable_live_browser_tools`.

## Tests

- `app/tests/test_url_guard.py` — scheme rejection, internal-address blocking
  (including the exact reported repro, metadata endpoints, IPv4-mapped IPv6,
  multi-record DNS), the allowlist staying port-scoped, the route handler, and
  `browser_inspect` refusing before Chromium launches. DNS is mocked; no network.
- `app/tests/test_clone_agent_removed.py` — the deleted modules stay deleted, no
  dangling imports in live agents, no unguarded `page.goto(`, no API tokens in logs.
