# LP-15: Working on a Leo app from Codex / ChatGPT through GitHub

Research only. Nothing here is built. Kody and Darren decide whether this is a
supported workflow, and on which plan, before any of the gaps below get built.

Asked for by Richie (Vice Theory Studios, Business plan, box `leo-loepo`, 2026-10-13).
He plans features in ChatGPT/Codex and wants Codex to commit straight to the app's
GitHub repo, while LlamaPress keeps hosting, the database, deploys, backups and domains.

## Short answer

It works today with no code changes, but only when the customer follows one rule:
**Push from the box before Codex starts, and Pull before Leo starts again.**
Break that rule once and the box is stuck: Pull and Push both fail, and the only
way out is a shell.

## How the pieces work today

| Piece | What it does | Where |
|---|---|---|
| GitHub sign-in | Device-code login; installs the user's token for `git` on the box (`repo` scope) | `app/routers/github_auth.py` |
| Push | `git push` of the whole app repo | `POST /api/git/push`, `app/routers/checkpoints.py` |
| Pull | `git pull --ff-only`, then restarts the Rails container | `POST /api/git/pull` |
| Updates badge | `git fetch`, shows how far behind/ahead the box is | `GET /api/git/remote-status` |
| Branch switch | Checks out a remote branch, pulls, restarts | `POST /api/git/checkout` |

The repo is the whole Leonardo checkout on the box (`rails/app`, `rails/db`, routes,
`bin/`, compose file, Leo's agent config). Commits land on the box from three places:
the **Save** button (checkpoint), Leo's `git_commit` tool, and `bin/update`'s
`chore(platform-sync)` commit on every fleet update.

## Round-trip proof

Run with the same git commands the endpoints run, against a local stand-in for
GitHub (a bare repo), with a "box" clone and a "Codex" clone. Not run against real
GitHub or a customer box: that would push to a real repo, and the Pull button
restarts the shared dev box's Rails app.

| Scenario | Pull | Push |
|---|---|---|
| A. Box clean, Codex commits | ✅ fast-forwards | — |
| B. Leo has unsaved edits to a *different* file, Codex commits | ✅ fast-forwards, edits kept | — |
| C. Leo has unsaved edits to the *same* file Codex changed | ❌ refused, friendly message | — |
| D. Box has a commit Codex doesn't have (Save, Leo commit, or fleet update), Codex commits | ❌ "Not possible to fast-forward" | ❌ rejected (non-fast-forward) |

**D is the real problem.** It isn't a merge conflict. Even when the two sides touched
different files, `--ff-only` refuses. The UI shows "you have file changes that conflict",
which is wrong and gives the customer nothing to do. Push fails too, so neither
button gets them out.

## Checking the ticket's listed breakages

| Ticket claim | Verdict |
|---|---|
| New migrations don't run after a pull | **Mostly false.** Pull restarts the Rails container, and its start command runs `rails db:prepare`, which applies pending migrations. They only stay unapplied if the restart fails (Pull says so), and then the app shows Rails' pending-migration page. Verified by reading the code, not run live. |
| New gems are blocked by the base image | **True, and silent.** The Gemfile isn't in the customer's repo at all; it lives in the image. If Codex adds `rails/Gemfile` or edits `rails/config/puma.rb` (it's in the repo but not mounted), the box ignores it and gives no error. |
| Leo's uncommitted edits conflict with pulls | **Partly.** Only when the same file changed (C). The bigger problem is committed work on the box (D). |
| `bin/update` overwrites some files | **True, small and known.** It overwrites five platform paths: `bin/`, `rails/db/migrate` (adds files, never deletes the app's own), `langgraph/langgraph.json`, `docker-compose.yml`, `rails/app/javascript/llamapress`. Codex edits there are lost on the next fleet update. Everything else (`rails/app`, routes, models, views) is safe. |

Two things the ticket doesn't list:

- **The box needs a GitHub repo first.** `origin` has to point at a repo the customer
  can write to. Today an admin sets that up by hand: auto-provisioning is planned, not
  built (`docs/github-repo-provisioning.md`).
- **The ChatGPT MCP connector is a different path.** It edits files on the box directly,
  like Leo does, and skips GitHub. It doesn't need this workflow. Not verified here: the
  connector lives on the mothership.

## Customer guide (as it works today)

1. **Get a repo.** Ask LlamaPress support to connect your app to a GitHub repo you own.
2. **Sign in to GitHub** in Leo: checkpoint panel → GitHub sign-in → enter the code.
3. **Push** from the checkpoint panel. Do this every time before Codex starts.
4. **Give Codex the repo** (Codex → connect GitHub → pick the repo). Tell it:
   - edit only `rails/app`, `rails/config/routes.rb`, and `rails/db/migrate`;
   - don't add gems; ask LlamaPress if you need one;
   - don't touch `bin/`, `docker-compose.yml`, `langgraph/`, or `rails/app/javascript/llamapress`.
5. **Merge Codex's work into the branch the box is on** (usually `main`).
6. **Pull** from the checkpoint panel. The app restarts and runs any new migrations.
7. Only then go back to Leo, and don't press Save or let Leo commit while Codex is working.

If Pull says it can't auto-update: the box and GitHub both moved. Contact support.
There's no self-serve fix yet.

## Gaps to build if founders say yes

| Gap | Size | What it fixes |
|---|---|---|
| Pull merges instead of fast-forward-only, and the message tells the truth | S | Case D is gone whenever the two sides touched different files: almost always. Same-file clashes still stop, with an honest message. |
| "Sync" button: commit box work, pull with merge, push | S–M | Removes the push-first/pull-first rule. Builds on the row above. |
| Conflict-resolution UI (or hand the conflict to Leo to fix) | L | The same-file case. Having Leo resolve it in chat is likely cheaper than a UI. |
| Warn on files the box ignores (`Gemfile`, unmounted config, allowlisted platform paths) after a pull | S | Turns silent no-ops and future overwrites into a visible notice. |
| Repo auto-provisioning | M | Removes the admin step. Already designed in `docs/github-repo-provisioning.md`. |
| Auto-migrate after pull | — | Not needed. The restart already does it. |
| Gem validation | M | Only worth it if per-app gems become a thing. Today the answer is "no new gems". |

**Recommendation:** if this is supported, the first two rows (merge-pull + honest
message, then Sync) are the whole job for Richie's case, about S–M total. Everything
else can wait for a second customer to ask.
