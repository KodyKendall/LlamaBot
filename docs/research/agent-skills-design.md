# Agent Skills for LlamaBot — Design Document

**Status:** Draft / proposal
**Author:** Claude (research + design session, 2026-06-23)
**Companion research:** [agent-skills-why-use-skills-anthropic.md](agent-skills-why-use-skills-anthropic.md)

---

## 1. Executive summary

Anthropic's **Agent Skills** are a deceptively simple idea: a skill is a folder with a
`SKILL.md` file. The agent always knows the skill's *name + description* (cheap, ~100
tokens), and only loads the full instructions **when it decides the skill is relevant** —
by calling a tool that reads the file and dumps its markdown into the conversation. That's
the whole trick. As the user intuited: *"activate skill tool → all it does is dump the
SKILL markdown into context."* The research below confirms this is exactly right, and the
elegance is in the discipline around it (progressive disclosure), not the mechanism.

LlamaBot **already has a `Skill` concept** ([app/models.py:61](../../app/models.py)) — but
it is a *static prompt-blob* that gets concatenated into the prompt chain on every message.
That is the pre-Skills "paste the same instructions every time" pattern that Agent Skills
were designed to replace. This document proposes evolving LlamaBot's Skill from a
"stackable prompt snippet" into **true progressive-disclosure Agent Skills**, invokable by
both the user (slash-style) and the model (autonomously), with bundled resources and a
clean authoring UI — implemented natively on our LangGraph + FastAPI stack.

---

## 2. First principles: how a skill actually works

Strip away the marketing and a Skill is three things layered by **when they load**:

| Level | What | When loaded | Token cost | In LlamaBot terms |
|-------|------|-------------|------------|-------------------|
| **L1 — Metadata** | `name` + `description` from YAML frontmatter | Always, at startup | ~100 tok/skill | Listed in a tool description or system prompt |
| **L2 — Instructions** | The `SKILL.md` body (workflows, rules) | When the skill is *invoked* | < 5k tok | Returned as a `ToolMessage` |
| **L3 — Resources** | Bundled files (`reference.md`, `scripts/*.py`, templates) | Only when referenced | ~0 until read | Read via existing `read_file` / `bash` tools |

### The actual mechanism (this is the "simple" part)

The critical, non-obvious finding from the deep-dive into Claude Code's internals:

> **Skills do NOT live in the system prompt.** A meta-tool named `Skill` appears in the
> model's `tools` array alongside `Read`, `Bash`, etc. Its tool description contains an
> `<available_skills>` list — each skill rendered as `"name": description`. The model reads
> that list and uses plain language understanding to decide when to fire.
>
> When the model calls the `Skill` tool with a skill name, the harness:
> 1. validates the skill exists / is permitted,
> 2. reads `SKILL.md` from the filesystem,
> 3. **injects the rendered markdown into the conversation as a new message**, and
> 4. optionally modifies execution context (pre-approves tools, switches model).
>
> The content then **stays in context for the rest of the session** — it is not re-read on
> later turns. (Claude Code, *How the Skill Tool Works*, lee hanchung deep dive.)

So "activate skill" is literally: **a tool call whose return value is the markdown file**.
No fine-tuning, no embeddings, no RAG. The model's own judgment (driven by the description)
is the router; the filesystem is the store; the tool-return channel is the loader.

### Why progressive disclosure matters

You can install *hundreds* of skills for ~100 tokens each. Only the handful the model
actually invokes ever pay the full cost. Bundled L3 files (a 10k-line API reference, a
dataset, a Python script) cost **zero tokens** until the model chooses to read or execute
them — and a script's *output*, not its source, is what enters context. This is what makes
the bundled content "effectively unbounded."

A useful mental model from Anthropic: **a skill is an onboarding guide for a new hire.**
The description is the table of contents; the body is the chapter; the bundled files are
the appendix you flip to only when needed.

---

## 3. How the top frameworks implement it

All major agent frameworks have converged on the **same `SKILL.md` format** — as of
**Dec 18, 2025** it is an open standard ([agentskills.io](https://agentskills.io)), donated
to the Linux-Foundation-backed Agentic AI Foundation. Skills authored once work across
Claude Code, the Claude API, OpenAI Codex, Gemini CLI, GitHub Copilot, Cursor, and 20+
other tools. The differences are in *discovery location*, *invocation control*, and
*execution environment*, not the file format.

### 3.1 Claude Code (filesystem-native)

- **Discovery:** scans `~/.claude/skills/<name>/SKILL.md` (personal),
  `.claude/skills/` (project), plugin dirs, and enterprise managed settings. Live file
  watching — adding/editing a skill takes effect mid-session.
- **Invocation:** model-invoked automatically *or* user-invoked via `/skill-name`. Custom
  slash commands and skills have **merged** — `/deploy` can come from either.
- **Frontmatter schema** (all optional except `description` is recommended):

  | Field | Purpose |
  |-------|---------|
  | `name` | Display name (defaults to directory name) |
  | `description` | What it does + **when to use it** (the router signal; capped ~1,536 chars) |
  | `when_to_use` | Extra trigger phrases, appended to description |
  | `disable-model-invocation` | `true` → only the user can invoke (e.g. `/deploy`) |
  | `user-invocable` | `false` → only the model can invoke (background knowledge) |
  | `allowed-tools` / `disallowed-tools` | Pre-approve / remove tools while active |
  | `model` / `effort` | Override model or reasoning effort for the skill |
  | `context: fork` + `agent` | Run the skill in an isolated subagent |
  | `argument-hint` / `arguments` | Named args for `$ARGUMENTS`, `$0`, `$name` substitution |
  | `paths` | Globs that auto-activate the skill only for matching files |
  | `hooks` | Lifecycle hooks scoped to the skill |

- **Dynamic context injection:** `` !`git diff HEAD` `` in the body runs a shell command at
  load time and inlines the output **before** the model sees the skill. This is
  preprocessing, not a tool call.
- **Lifecycle:** loaded content persists; on auto-compaction the most recent invocation of
  each skill is re-attached (first 5k tokens each, 25k combined budget).
- **Budgeting:** skill descriptions consume ~1% of the context window by default; when many
  skills overflow, least-used descriptions are trimmed/dropped first.

### 3.2 Claude API / Agent SDK (sandboxed)

- Skills run inside the **code-execution container**. Enabled via beta headers
  (`skills-2025-10-02`, `code-execution-2025-08-25`, `files-api-2025-04-14`) and referenced
  by `skill_id` in the `container` param.
- Pre-built skills: `pptx`, `xlsx`, `docx`, `pdf`. Custom skills uploaded via `/v1/skills`,
  shared workspace-wide.
- **Constraint:** no network access, no runtime package installs — only pre-installed deps.
  (claude.ai has variable network access; Claude Code has full local access.)

### 3.3 OpenAI Codex (same format, different paths)

- A skill is the **same** `SKILL.md` folder. Codex scans `.agents/skills/` from cwd up to
  repo root, plus user/admin/system locations.
- **Invocation:** explicit via `$skill-name` (note the `$` sigil vs Claude's `/`), or
  automatic selection.
- **AGENTS.md vs Skills:** `AGENTS.md` = always-on repo context (setup, test commands,
  standards), read once per task. Skills = on-demand instruction packs. *This distinction
  maps cleanly onto LlamaBot's existing `LEONARDO.md` (always-on) vs the proposed Skills
  (on-demand).*

### 3.4 The convergent takeaway

The format is settled and portable. **If LlamaBot adopts the `SKILL.md` standard, users'
skills are portable to/from Claude Code, Codex, and Cursor** — a real differentiator for a
product whose users live in those tools too.

---

## 4. Where LlamaBot stands today (the gap)

LlamaBot has the *vocabulary* of skills but not the *mechanism*.

**What exists:**
- `Skill` SQLModel table ([app/models.py:61-77](../../app/models.py)) — `name`, `content`,
  `description`, `group`, `is_active`, `usage_count`. Shared library, all users.
- A service layer (`skill_service.py`) with CRUD.
- The model's own docstring states the behavior: *"Skills are injected after the selected
  prompt in the message chain. Multiple skills can be selected at once (multi-select)."*

**What that actually is:** a **static prompt-blob stack**. The user manually multi-selects
skills in the UI; their full `content` is concatenated into the prompt on *every* message.
This is precisely the anti-pattern Agent Skills replaced:

| Dimension | LlamaBot today | True Agent Skills |
|-----------|----------------|-------------------|
| Who decides relevance | **User**, manually, per-message | **Model**, autonomously, by description |
| When content loads | Always (every message) | Only when invoked (progressive disclosure) |
| Token cost | Full `content` × every turn | ~100 tok metadata until invoked |
| Bundled resources | None (single `content` text field) | Scripts, references, templates, datasets |
| Scale ceiling | A few skills before context bloat | Hundreds of skills, near-zero idle cost |
| Portability | LlamaBot-only DB rows | Standard `SKILL.md`, cross-tool |

**Where the relevant plumbing lives** (integration points for the new design):

- System prompt assembly: `app/agents/leonardo/project_context.py:58-132`
  (`build_system_prompt_with_project_context`) — loads `LEONARDO.md`, `MEMORY.md`, etc.
- Tool definitions: `app/agents/leonardo/rails_agent/tools.py` (`@tool` decorator → returns
  `Command` + `ToolMessage`).
- Tool registration: `app/agents/leonardo/rails_agent/nodes.py:91-102` (`default_tools`
  list passed to `create_agent`).
- Filesystem + execution: agent runs in the LlamaBot container with the Rails app mounted;
  `read_file` ([tools.py:244](../../app/agents/leonardo/rails_agent/tools.py)) and
  `bash_command` ([tools.py:1170](../../app/agents/leonardo/rails_agent/tools.py)) already
  give exactly the L3 access skills need.
- Slash commands (the user-invocation analog): backend registry
  `app/routers/slash_commands.py:56-167`; frontend dropdown
  `app/frontend/chat/ui/SlashCommandManager.js`.
- Prompt library UI (the management-UI analog to copy): `PromptManager` in
  `app/frontend/chat/index.js:324-333`.

**The punchline:** LlamaBot has *every primitive* needed — a tool-return channel, a
filesystem, a code-execution tool, a system-prompt injection point, a DB-backed library,
and a slash-command UI. The work is wiring, not invention.

---

## 5. Proposed design

### 5.1 Goals & non-goals

**Goals**
- Model-invoked **and** user-invoked skills with progressive disclosure.
- `SKILL.md` open-standard compatibility (import/export portability).
- Bundled L3 resources (reference docs + executable scripts).
- A no-code authoring UI for non-technical LlamaBot users.
- Backwards compatibility with the existing prompt-blob `Skill` rows.

**Non-goals (v1)**
- Per-skill subagent forking (`context: fork`) — defer to v2.
- Lifecycle hooks, `paths` auto-activation — defer.
- Cross-surface sync with claude.ai/API skill stores — out of scope.

### 5.2 Storage model — DB + materialized filesystem (hybrid)

LlamaBot's users are non-technical and multi-tenant (shared library), so a pure
filesystem-only model (like Claude Code) is the wrong primary interface — but the
filesystem is what makes L3 bundled resources and the open standard work. **Use both:**

- **DB is the source of truth for authoring & discovery.** Extend the `Skill` table:

  ```python
  class Skill(...):
      # existing: id, name, content, description, group, is_active, usage_count
      slug: str               # kebab-case, filesystem-safe, unique  → the invocation name
      when_to_use: Optional[str]      # extra trigger phrases (open-standard field)
      allowed_tools: Optional[str]    # space/comma list, pre-approved while active
      disable_model_invocation: bool = False   # user-only (e.g. /deploy)
      user_invocable: bool = True
      invocation_mode: str = "agent_skill"  # "agent_skill" | "legacy_blob" (migration)
      # `content` now holds the SKILL.md *body*; frontmatter is derived from columns
  ```

- **Resources table** for L3 bundled files:

  ```python
  class SkillResource(...):
      id: int
      skill_id: int           # FK
      filename: str           # e.g. "reference.md", "scripts/validate.py"
      content: str            # text; binary via object storage if needed later
      is_executable: bool = False
  ```

- **Materialization:** on skill create/update, write the skill to disk in open-standard
  layout under a per-instance skills root, e.g.
  `/<workdir>/.leonardo/skills/<slug>/SKILL.md` + bundled files. This is what the model's
  `read_file` / `bash` tools point at for L3, and what makes export trivial (zip the dir).
  Frontmatter is rendered from the DB columns so the on-disk file is a valid, portable
  `SKILL.md`.

> **Decision needed:** is the skills root per-instance (shared across all users, matching
> today's "all users share the library") or per-user? Recommend **per-instance shared** for
> v1 to match current semantics; revisit when multi-tenancy hardens. See §7.

### 5.3 The `activate_skill` meta-tool (the core mechanism)

Add one LangChain tool to every agent's `default_tools`. This *is* the "dump the markdown"
mechanism, expressed in LlamaBot's existing tool-return idiom:

```python
@tool(description=ACTIVATE_SKILL_DESCRIPTION)  # description is rendered dynamically — see §5.4
def activate_skill(skill_slug: str, runtime: ToolRuntime) -> Command:
    """Load a skill's full instructions into context. Call this the moment a user
    request matches one of the available skills listed in this tool's description."""
    skill = skill_service.get_by_slug(skill_slug)
    if not skill or not skill.is_active:
        return _tool_error(f"No active skill '{skill_slug}'.")
    skill_service.increment_usage(skill.id)
    body = render_skill_markdown(skill)   # frontmatter + body + "resources live at <path>"
    return Command(update={"messages": [
        ToolMessage(content=body, tool_call_id=runtime.tool_call_id)
    ]})
```

Mechanically identical to Claude Code: the tool's **return value is the SKILL.md content**,
which the LLM then sees on the next turn and follows. Bundled L3 files are *not* inlined —
the body references them by path (`See .leonardo/skills/<slug>/reference.md`) and the model
pulls them with the existing `read_file` tool only if needed. Scripts run via `bash_command`
so only their *output* enters context.

**Why a tool and not system-prompt injection:** putting full skill bodies in the system
prompt defeats progressive disclosure (every skill, every turn). The tool-return channel
gives us exactly the load-on-demand + persists-in-context lifecycle that defines a skill,
and reuses LlamaBot's existing streaming/tool-call rendering on the frontend for free.

### 5.4 Metadata injection (L1) — dynamic tool description

At agent-build time, fetch active, model-invocable skills and render them into the
`activate_skill` tool's description:

```
Load specialized instructions on demand. Available skills:
<available_skills>
- rails-migration: Safely write & run Rails DB migrations. Use when the user asks to
  add/change a column, table, or index, or mentions migrations.
- stripe-checkout: Implement Stripe Checkout in a Rails app. Use when the user mentions
  payments, billing, subscriptions, or Stripe.
  ...
</available_skills>
Call activate_skill(skill_slug=...) with the matching slug.
```

This is rebuilt per request in `get_langgraph_app_and_state`
([request_handler.py:1335](../../app/websocket/request_handler.py)) so newly authored skills
appear immediately (LlamaBot's analog of Claude Code's live file watching). Apply the same
**character budget discipline** the standard uses: cap each description, and if the library
grows large, trim least-used (`usage_count`) descriptions first to bound token cost.
Skills with `disable_model_invocation=True` are **omitted** from this list (user-only).

### 5.5 User invocation — fold into slash commands

Reuse the existing slash-command UX so users get a familiar `/skill-name` affordance:

- `SlashCommandManager` already renders a `/` dropdown from `GET /api/slash-commands`.
  Extend that endpoint (or add `GET /api/skills`) so `user_invocable` skills appear in the
  dropdown, tagged as skills.
- Selecting `/rails-migration` sends the message with an explicit
  `invoke_skill: "rails-migration"` field. The backend, in the request handler, **prepends a
  synthetic `activate_skill` tool call** (or directly injects the rendered body as a
  `ToolMessage`) before the agent runs — guaranteeing load regardless of model choice.
- Arguments after the slash (`/fix-issue 123`) map to the open standard's `$ARGUMENTS` /
  `$0` substitution, rendered into the body at injection time.

This unifies the two invocation paths onto one loader and matches the Claude Code /
Codex merge of "commands and skills are the same thing."

### 5.6 Authoring UI

Model the authoring experience on the existing `PromptManager` library picker, but richer:

- **Skill list** grouped by `group`, with active toggle and usage count.
- **Skill editor:** name/slug, description, `when_to_use`, the markdown body (with a live
  hint that good descriptions drive auto-invocation), invocation controls
  (`disable_model_invocation`, `user_invocable`), `allowed_tools` multiselect.
- **Resources tab:** add/edit bundled files (`reference.md`, `scripts/*.py`).
- **Import / Export:** upload a `SKILL.md` folder (zip) or paste a `SKILL.md`; export any
  skill as a standard zip. This is the portability hook to Claude Code / Codex.
- **"Generate skill from this conversation"** (v2): the standard's recommended authoring
  loop — ask the agent to distill a successful trajectory into a reusable `SKILL.md`. We
  already have the conversation + a capable model; this is a high-value, low-cost add.

### 5.7 Security & multi-tenancy

Skills are executable instructions — treat them like installing software (per Anthropic's
own guidance). For LlamaBot's shared-library, multi-user reality:

- **Authoring is privileged.** Gate skill create/edit behind `engineer_or_admin_required`
  (the same guard slash-command execution uses,
  [slash_commands.py:313](../../app/routers/slash_commands.py)). Regular `user`-role
  accounts can invoke but not author. This prevents a low-trust user from planting a skill
  that exfiltrates data or runs destructive bash for everyone.
- **`allowed-tools` is a grant, not a sandbox.** Honor it for pre-approval but keep
  baseline permission checks; never let a skill silently widen `bash_command` to
  destructive operations without the existing confirmation gates.
- **No untrusted dynamic fetches in v1.** Defer Claude Code's `` !`cmd` `` load-time shell
  injection — it's powerful but a real injection surface in a hosted multi-tenant product.
  If added later, gate it behind the same `disableSkillShellExecution`-style instance
  setting.
- **Audit:** log skill invocations to `CommandHistory` (or a `SkillInvocation` table) the
  way slash commands are logged, for traceability.

---

## 6. Phased implementation plan

Each phase is independently shippable and testable behind the existing CI gates
(spec + pytest + mock-LLM e2e). Per CLAUDE.md: **bug fix / behavior change = failing test
first**; assert structure (rows, fields, tool-call emitted), never exact LLM text.

**Phase 0 — Schema & migration (no behavior change)**
- Add the new `Skill` columns + `SkillResource` table.
- Backfill `slug` from `name`; set existing rows `invocation_mode="legacy_blob"` so current
  multi-select behavior is untouched. *Tests:* migration applies; legacy skills still
  concatenate as before.

**Phase 1 — The loader tool (model invocation)**
- Implement `activate_skill` + `render_skill_markdown`; add to `default_tools`.
- Render the dynamic `<available_skills>` description at agent-build time.
- *Tests:* tool returns a `ToolMessage` containing the body for a known slug; unknown slug
  errors; description lists only `user_invocable`/model-invocable active skills.

**Phase 2 — User invocation via slash**
- Surface `user_invocable` skills in the `/` dropdown; handle `invoke_skill` in the request
  handler by injecting the body. Wire `$ARGUMENTS` substitution.
- *Tests:* `invoke_skill` payload causes the body to be injected before the agent runs.

**Phase 3 — Filesystem materialization & L3 resources**
- On save, write `SKILL.md` + resources to `.leonardo/skills/<slug>/`. Reference resources
  by path in the body so `read_file`/`bash_command` reach them.
- *Tests:* save writes a valid `SKILL.md`; body references resolve to real files.

**Phase 4 — Authoring UI + import/export**
- Skill editor, resources tab, zip import/export.
- *Tests (frontend smoke / e2e):* create → invoke round-trip; import a standard `SKILL.md`.

**Phase 5 (v2) — Polish**
- "Generate skill from conversation", usage-based description budgeting, optional
  `context: fork` subagent execution, `paths` auto-activation.

---

## 7. Open questions / decisions for the team

1. **Skills root scope:** per-instance shared (recommended for v1, matches today) vs
   per-user. Affects materialization path and `allowed-tools` blast radius.
2. **Keep legacy prompt-blob skills indefinitely, or migrate/deprecate** once Agent Skills
   ship? Recommend keeping `invocation_mode="legacy_blob"` as a supported mode, since some
   users *want* always-on snippets (that's really "extra `LEONARDO.md`" content).
3. **Load-time shell injection (`` !`cmd` ``):** ship never / behind admin flag / freely?
   Recommend **behind admin flag** given the hosted multi-tenant threat model.
4. **Per-skill `model` override:** route through `get_llm`
   ([llm_factory.py](../../app/agents/leonardo/llm_factory.py)) — easy, but interacts with
   Darren's default-model policy. Worth it for v1?
5. **Does a skill's content count against summarization/compaction budgets** the way
   Claude Code re-attaches the first 5k tokens post-compaction? LlamaBot's
   `SummarizationMiddleware` would need a parallel "re-attach active skills" rule to avoid
   skills silently dropping out of long conversations.

---

## 8. Appendix — `SKILL.md` reference (open standard)

```markdown
---
name: rails-migration
description: Safely write and run Rails database migrations. Use when the user asks to
  add or change a column, table, or index, or mentions "migration".
when_to_use: schema change, add column, rename table, add index, db:migrate
allowed-tools: Bash(bin/rails db:migrate) read_file edit_file
---

# Rails migration

## Steps
1. Generate the migration: `bin/rails g migration <Name>`
2. Edit the migration; prefer reversible `change` methods.
3. Run `bin/rails db:migrate`; verify `schema.rb` updated.
4. If it fails, `bin/rails db:rollback` and fix.

## Gotchas
- Never edit a migration that has already run in production — add a new one.
- For large tables, see [reference.md](reference.md) for zero-downtime patterns.
```

Layout on disk (materialized from DB):

```
.leonardo/skills/rails-migration/
├── SKILL.md          # frontmatter (from columns) + body (Skill.content)
├── reference.md      # L3 — read on demand via read_file
└── scripts/
    └── check_pending.rb   # L3 — run via bash_command, only output enters context
```

---

## Sources

- [Equipping agents for the real world with Agent Skills — Anthropic](https://www.anthropic.com/engineering/equipping-agents-for-the-real-world-with-agent-skills)
- [Agent Skills overview — Claude Docs](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview)
- [Extend Claude with skills — Claude Code Docs](https://code.claude.com/docs/en/skills)
- [Claude Agent Skills: A First Principles Deep Dive — lee hanchung](https://leehanchung.github.io/blogs/2025/10/26/claude-skills-deep-dive/)
- [Agent Skills open standard — agentskills.io](https://agentskills.io)
- [Anthropic Introduces "Agent Skills" as Open AI Standard (Dec 2025)](https://opentools.ai/news/anthropic-introduces-agent-skills-as-open-ai-standard-a-new-era-of-cross-platform-portability)
- [Agent Skills — Codex / OpenAI Developers](https://developers.openai.com/codex/skills)
- [Custom instructions with AGENTS.md — Codex](https://developers.openai.com/codex/guides/agents-md)
- [anthropics/skills (public skills repo)](https://github.com/anthropics/skills)
