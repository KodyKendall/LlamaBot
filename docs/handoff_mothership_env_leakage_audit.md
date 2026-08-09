# Handoff: audit secret exposure in the Rails container across real instances

**Owner:** Mothership Leo
**Requested by:** Kody, 2026-08-09
**Status:** open

## Why this task exists

The agent writes and runs code inside the Rails container. Code that runs in a
container can read that container's environment variables. A customer can ask
the agent to print `ENV["ANTHROPIC_API_KEY"]` from a controller, or to run
`printenv` in a shell. Any secret present in that container is therefore a
secret the customer can read.

The Rails container loads the whole `.env` file through `env_file: .env` in
`docker-compose.yml`. That file holds credentials the Rails app never uses,
including every LLM provider key. LLM means large language model. Those keys bill
to LlamaPress, not to the customer, so a leak costs money directly.

## What has already been done

Three changes shipped on the dev box on 2026-08-09. Do not repeat them.

1. **File tools cannot leave the project.** `read_file`, `write_file`,
   `edit_file`, glob, grep and `ls` in `rails_agent/tools.py` had no guard against
   `..` in a path. `read_file("../../leonardo/.env")` returned the real `.env`.
   Those tools now resolve the path first and refuse anything outside the Rails
   project.
2. **Shell commands no longer inherit secrets.** `bash_command` runs through the
   Docker exec API. Docker adds the `Env` values in an exec request on top of the
   container's environment instead of replacing them, so every shell saw all the
   secrets. `build_exec_env()` now sets an empty value for every variable that is
   not on a small allowlist.
3. **The Rails container no longer receives 15 secrets.** `docker-compose.yml`
   sets an empty value for `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`,
   `GMI_DEEPSEEK_API_KEY`, `FIREWORKS_DEEPSEEK_API_KEY`, `GOOGLE_API_KEY`,
   `GEMINI_API_KEY`, `ALIBABA_API_KEY`, `META_API_KEY`, `MODEL_API_KEY`,
   `BEDROCK_API_KEY`, `TAVILY_API_KEY`, `GROUND_ROUTE_SEARCH_API_KEY`,
   `VSCODE_PASSWORD`, `AWS_KEY` and `AWS_PASS`. Values set under `environment:`
   override values from `env_file:`.

## The gap this task must close

Change 3 was verified against one codebase only: `~/dev/Leonardo/rails` and the
`llama_bot_rails` gem on the dev box. Around 45 customer instances run their own
Rails code. A customer app may read a variable that is now empty. That app will
break, and the failure may be silent.

Two variables were deliberately left in place because a decision is needed. See
"Questions to answer" below.

## Risk classification agreed with Kody

Not every secret matters equally.

**Acceptable to leave in the Rails container.** These are unique to one instance.
A customer reading their own value gains nothing they did not already own.

- `POSTGRES_PASSWORD`, `DATABASE_URL`, `DB_URI`
- `SECRET_KEY_BASE`, `RAILS_MASTER_KEY`
- `REDIS_URL`

**Not acceptable.** These are shared across the fleet, or they grant access
beyond the customer's own app.

- Every LLM provider key. One customer can spend LlamaPress credit for all.
- `AWS_KEY` and `AWS_PASS`. Access reaches beyond one instance.
- `VSCODE_PASSWORD`. Grants shell access to the box.
- `LLAMAPRESS_AI_LOGIN_SECRET`. Signs single sign-on tokens.
- `SCHEDULER_TOKEN`, `WS_SECRET_KEY`, `AUTH_DB_URI`.

## Tasks

Do these in order.

### Task 1. Measure real usage across the fleet

For every running customer instance, search the customer's Rails code for reads of
environment variables. Search for both `ENV["NAME"]` and `ENV.fetch("NAME")`.
Include `app/`, `lib/`, `config/` and any initializer.

Produce one table with these columns:

- instance name
- variable name
- file and line where the customer reads the variable

Report every instance that reads any variable listed in "Not acceptable" above.
Report `AWS_KEY`, `AWS_PASS` and `OPENAI_API_KEY` separately, because those three
are the most likely to be in real use.

**Warning:** do not print any variable's value into a report, a log or a ticket.
Report only names, files and line numbers.

### Task 2. Confirm the blanking did not break anything

For each running instance, after the next `bin/update` reaches it:

1. Confirm the Rails app answers HTTP 200 or 302 on `/`.
2. Confirm no new exception mentions a missing environment variable.
3. Report any instance whose error rate rises after the update.

### Task 3. Decide the remaining two variables

See "Questions to answer" and bring a recommendation with evidence from Task 1.

### Task 4. Check the other containers

This work covered the Rails container only. Confirm whether the code-server
container (`code`) also loads the full `.env`. That container gives the customer a
terminal, so the same exposure applies. Report what it loads today.

## AUDIT RESULT, 2026-08-09 (this section supersedes the tasks above)

Mothership Leo probed 97 running boxes. The audit answered the open questions and
changed the blank list. The findings below are now the source of truth.

### The blank list changed

Three keys were removed from the list because customer apps call them
server-side. Blanking them would have broken paying customers at runtime, not at
boot, because every read is nil-tolerant `ENV["..."]`.

| Key | Who breaks | Status |
|---|---|---|
| `OPENAI_API_KEY` | 13 boxes use profile images or audio. About 8 more have custom features. | Kept |
| `DEEPSEEK_API_KEY` | leasure-group-lxd (4 services), lohman, lohman-dev, social-support-society and its dev twin. All paying. | Kept |
| `GEMINI_API_KEY` | leasure-group-lxd | Kept |

Six keys were added to the list. Their only readers run in the LlamaBot
container, which keeps the whole `.env`, so blanking the Rails copy changes no
behaviour: `LLAMAPRESS_AI_LOGIN_SECRET`, `SCHEDULER_TOKEN`, `WS_SECRET_KEY`,
`AUTH_DB_URI`, `TAVILY_API_KEY`, `GROUND_ROUTE_SEARCH_API_KEY`.

`AWS_KEY` and `AWS_PASS` stay on the list. No probed box defines them, so
blanking costs nothing.

### The largest hole is not the environment

The `code` container loads no `env_file`, but it mounts the whole project at
`./:/config/workspace`. The raw `.env` FILE is therefore readable, and writable,
from the customer's editor terminal on 96 of 97 boxes. An old `.env.bak` sits on
the same mount. The `llamabot` container carries the same full-repo mount at
`/app/leonardo`.

Environment blanking does not touch file-level exposure. Until the mount changes,
a customer with the editor open can read every secret.

### Delivery has not started

Zero of 97 boxes have the compose fix, because it is not on upstream `main` yet.
Instance image tags range from 0.4.0c to 0.6.5, and 42 boxes still run 0.4.0x, so
a fleet update push is needed regardless.

## Remaining work, in priority order

1. **Close the `code` container mount.** Mount only the subdirectories the editor
   needs, or move `.env` outside the project directory and point compose at the
   new path. This is the only change that stops a customer reading the file
   directly. Treat every other item as secondary.
2. **Delete the `.env.bak` files that hold live secrets** from every box.
3. **Merge the compose change and push a fleet update.** Then re-run
   `secaudit-sweep.py` and compare against the recorded baseline: 73 boxes at
   HTTP 200, 24 at HTTP 302, zero env-missing errors.
4. **Build the LlamaBot proxy endpoint** so customer apps can call models without
   holding a key. Migrate the affected boxes. Only then blank `OPENAI_API_KEY`,
   `DEEPSEEK_API_KEY` and `GEMINI_API_KEY`.
5. **Rotate every key that the fleet has already exposed.** Assume all listed
   secrets are known to customers who looked.

## Original questions, now answered

1. **`OPENAI_API_KEY`.** Kody's position is that the Rails container should not
   receive it by default. Today `rails/app/services/open_ai.rb` reads it, and
   `users_controller.rb` calls that service to generate profile images and audio.
   Blanking the key breaks those two features. Answer: how many real instances
   call `OpenAi.new`? If the number is zero or near zero, blank the key and move
   the feature to a LlamaBot endpoint.
2. **`LLAMAPRESS_AI_LOGIN_SECRET`.** Nothing in the dev box's Rails code reads it,
   but single sign-on is a recent feature and may read it through another path.
   Answer: does any Rails or gem code read it? If no code reads it, blank it.

## How to verify a fix on one instance

Run this inside the Rails container. The command prints lengths only, never
values.

```sh
for v in ANTHROPIC_API_KEY AWS_KEY VSCODE_PASSWORD OPENAI_API_KEY DATABASE_URL; do
  eval "len=\${#$v}"
  printf "%-24s len=%s\n" "$v" "$len"
done
```

A blanked variable prints `len=0`. `DATABASE_URL` must stay non-zero, or Rails
cannot reach its database.

## Known limits of the current fix

State these plainly in any summary. Do not describe the system as airtight.

1. A customer can still read any secret that remains in the Rails container.
   `SECRET_KEY_BASE` and the database URL are still readable by design.
2. The fix depends on `docker-compose.yml`. That file is on `bin/update`'s sync
   allowlist, so an instance only receives the fix after its next update. Report
   how many instances still run an old compose file.
3. `bash_command` still carries a substring blocklist for `.env` and `ENV[`. That
   blocklist is not load bearing and does not stop a determined user. The
   environment scrub is what protects the shell.

## Reference

- Change 1 and 2: `LlamaBot/app/agents/leonardo/rails_agent/tools.py`
- Change 3: `Leonardo/docker-compose.yml` and `Leonardo/docker-compose-dev.yml`
- Tests: `LlamaBot/app/tests/test_file_tool_path_containment.py`,
  `LlamaBot/app/tests/test_bash_exec_env_scrub.py`
