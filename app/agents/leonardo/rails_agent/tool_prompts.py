"""
Archived tool prompts that may be useful for future features.
These were removed from the main prompts.py to reduce context size,
but are preserved here for when git/github functionality is re-enabled.
"""

# =============================================================================
# GIT TOOLS (for RAILS_AGENT_PROMPT)
# =============================================================================

GIT_TOOLS_SECTION = """
### `git_status`
Purpose: check the status of the git repository to see latest changes & uncommitted changes

Usage:
- Use this tool to check the status of the git repository to see latest changes & uncommitted changes
- This tool will return a HTML page with the status of the git repository
- This tool will return a HTML page with the latest changes & uncommitted changes
- This tool will return a HTML page with the uncommitted changes

### `git_commit`
Purpose: commit the changes to the git repository

Usage:
- Use this tool to commit the changes to the git repository
- This tool will return a HTML page with the status of the git repository
- This tool will return a HTML page with the latest changes & uncommitted changes
- This tool will return a HTML page with the uncommitted changes

### `github_cli_command`
Purpose: use the github cli to interact with github, and set up the authentication between the user's github account and the git command tool.
Usage:
- Authenticate git command tool with the user's github account, by following these steps:

#### Instructions for authenticating github with git for pushing to github
<INSTRUCTIONS_FOR_AUTHENTICATING_GITHUB_WITH_GIT_FOR_PUSHING_TO_GITHUB>
Here's a step-by-step, copy-ready prompt to instruct a future LLM or agent on how to achieve reliable, non-interactive git pushes to GitHub using the GitHub CLI as a credential helper in a Rails project environment (where SSH is problematic or unavailable):

Prompt: Enable Automated GitHub Pushes via GitHub CLI Credential Helper in a Rails Project

Perform the following steps to ensure the git CLI can push to a GitHub repository using the credentials managed by the GitHub CLI (gh):

Check the Current Git Remote

Run: git remote -v
If the remote URL is not HTTPS (e.g., set to SSH), update the remote to use HTTPS:
git remote set-url origin https://github.com/[USERNAME]/[REPO].git
(Replace with actual username/repo as appropriate.)
Ensure GitHub CLI Authentication

Verify that gh auth status reports the correct GitHub account, with sufficient scopes for repo operations.
Configure git to Use gh as a Credential Helper

Run:
git config --global credential."https://github.com".helper "!gh auth git-credential"
Test the Configuration

Attempt to push code to GitHub:
git push
Confirm that the push succeeds without credential prompts or errors.
If You Encounter SSH or Host Verification Errors:

Double-check that the remote is set to HTTPS, not SSH.
Only SSH users need to manage known_hosts and key distribution.
Summary

The environment should now support non-interactive git push/git pull using GitHub CLI credentials, ideal for CI and container use.
- This tool will return a HTML page with the status of the github repository
- This tool will return a HTML page with the latest changes & uncommitted changes
- This tool will return a HTML page with the uncommitted changes
</INSTRUCTIONS_FOR_AUTHENTICATING_GITHUB_WITH_GIT_FOR_PUSHING_TO_GITHUB>
"""

# =============================================================================
# STANDALONE TOOL DESCRIPTIONS
# =============================================================================

GIT_STATUS_DESCRIPTION = """
Use this tool to check the status of the git repository.

Usage:
- You can use this tool to check the status of the git repository.
"""

GIT_COMMIT_DESCRIPTION = """
Use this tool to commit the changes to the git repository.

Usage:
- The message parameter must be a string that is a valid git commit message.
- You can use this tool to commit the changes to the git repository.
"""

GIT_COMMAND_DESCRIPTION = """
Use this tool if you need to use git for things other than git_commit and git_status. For example, git push, or git pull, or configure the git repository, such as setting the author name and email, etc.

This takes an input argument of the string arguments to pass to the git command.

For example, if you need to run "git config", then you would pass the following string to the tool:
<EXAMPLE_INPUT>
config --global user.name "Leonardo"
</EXAMPLE_INPUT>

If you need to run "git add", then you would pass the following string to the tool:
<EXAMPLE_INPUT>
add .
</EXAMPLE_INPUT>

If you need to run "git commit", then you would pass the following string to the tool:
<EXAMPLE_INPUT>
commit -m "Add new feature"
</EXAMPLE_INPUT>

Please note that the author information should be set, if it's not, then please set it to the following.

<CONFIGURE_USER_INFO>
config --global user.name "Leonardo"
config --global user.email "leonardo@llamapress.ai"
</CONFIGURE_USER_INFO>

If there are issues with the git repository directory, you can use this tool to mark the safe directory inside our docker container, which may be necessary.

Here is the command to do so:
<COMMAND_TO_MARK_SAFE_DIRECTORY>
config --global --add safe.directory /app/leonardo
</COMMAND_TO_MARK_SAFE_DIRECTORY>

If you need to push the changes to the remote repository, you can use the following command:
<EXAMPLE_INPUT>
push
</EXAMPLE_INPUT>

If you need to pull the changes from the remote repository, you can use the following command:
<EXAMPLE_INPUT>
pull
</EXAMPLE_INPUT>

If you'd need to make a new branch to isolate features, or if the user asks, you use the argument "branch" to create a new branch.
<EXAMPLE_INPUT>
branch
</EXAMPLE_INPUT>

Usage:
- The command parameter must be a string that is a valid git command argument.
- You can use this tool to configure the git repository, or do anything else.
- Please use this tool to run commands NOT covered by the other git tools.
- By default, you should not use this tool for git commit, or for git status, you should use the other git tools for these such as git_commit and git_status tool-calls.
"""

GITHUB_CLI_DESCRIPTION = """
The `github_cli_command` used when you need to check the github connection to this repo, or get information about the repo, or do anything else related specifically to github.

If the git push command fails due to authentication issues, you can use this tool to authenticate with github. For example:
`gh auth setup-git` would be:
<EXAMPLE_ARGUMENT>
auth setup-git
</EXAMPLE_ARGUMENT>

This takes an input argument of the string arguments to pass to the github command.

For example, if you need to check the github connection to this repo, you can use the following command:
gh repo view
Would be:
<EXAMPLE_ARGUMENT>
repo view
</EXAMPLE_ARGUMENT>

gh status
Would be:
<EXAMPLE_ARGUMENT>
status
</EXAMPLE_ARGUMENT>

## IMPORTANT: If the user isn't authenticated with the gh cli, we need to explain how to authenticate with the gh cli.
The user has two options to authenticate with the gh cli:
1. SSH into the server, and run `gh cli auth` to manually authenticate with the gh cli
2. Ask the user to go to: rails.auth.llamapress.ai/users/sign_up, then register an account, then click on "Connect Github".
Then, they will click on "Reveal". From there, ask the user to paste in their access token in their message to you.

If the user sends in their github access token, use the following command to authenticate with the gh cli.

gh auth status >/dev/null 2>&1 || echo "<their_token>" | gh auth login --with-token; gh repo list
Would be:
<EXAMPLE_ARGUMENT>
auth status >/dev/null 2>&1 || echo "gh_xxx_token" | gh auth login --with-token; gh repo list
</EXAMPLE_ARGUMENT>

Usage:
- The command parameter must be a string that is a valid github command argument.
- You can use this tool to check the github connection to this repo, or get information about the repo, or do anything else related specifically to github.
"""

# =============================================================================
# INTERNET SEARCH (for future reference)
# =============================================================================

INTERNET_SEARCH_DESCRIPTION = """
Usage:
- The query parameter must be a string that is a valid search query.
- You can use this tool to search the internet for information.
"""

INTERNET_SEARCH_SECTION_FOR_PROMPT = """
### `internet_search`
Purpose: search the web for authoritative information (Rails docs, API changes, gem usage, security guidance).
Parameters (typical):
- `query` (required): free-text query.
- `num_results` (optional): small integers like 3–8.
- `topic` (optional): hint string, e.g., "Rails Active Record".
- `include_raw` (optional): boolean to include raw content when you need to quote/verify.
Usage:
- Use when facts may be wrong/outdated in memory (versions, APIs, gem options).
- Summarize findings and record key URLs; link in `final_report.md` only if they help operators/users.
"""

# =============================================================================
# VIEW PAGE TOOL (for future reference)
# =============================================================================

# =============================================================================
# LEONARDO.MD TOOLS
# =============================================================================

READ_LEONARDO_MD_DESCRIPTION = """Read the LEONARDO.md project context file.
This file contains project-specific instructions, context, and configuration that guides your behavior.
It lives at .leonardo/LEONARDO.md and is always loaded into your system prompt.
Use this to see the current contents before making edits."""

EDIT_LEONARDO_MD_DESCRIPTION = """Edit the LEONARDO.md project context file by replacing text.
Parameters:
- old_string: The exact text to find and replace (must be unique in the file)
- new_string: The text to replace it with

Use this when the user asks you to update project instructions, add context, or modify LEONARDO.md.
The file is at .leonardo/LEONARDO.md and changes will take effect on the next conversation."""

WRITE_LEONARDO_MD_DESCRIPTION = """Create or completely overwrite the LEONARDO.md project context file.
Parameters:
- content: The full content to write to LEONARDO.md

Use this only when creating LEONARDO.md for the first time or when the user wants a complete rewrite.
Prefer edit_leonardo_md for partial changes."""

# =============================================================================
# MEMORY TOOLS
# =============================================================================

SAVE_MEMORY_DESCRIPTION = """Save information to long-term memory that persists across conversations.

Use this when:
- The user explicitly says "remember this", "don't forget", or similar
- The user corrects your behavior and you should remember the correction
- The user states preferences about how they want things done
- Important project context that should persist across sessions

Do NOT save:
- Routine task details or temporary debugging info
- Information already in LEONARDO.md or MEMORY.md
- Trivial or obvious information

Parameters:
- name: Short descriptive name (e.g., "prefers-tailwind-over-bootstrap")
- description: One-line summary of what this memory contains
- memory_type: One of "user", "feedback", "project", "reference"
  - user: Role, preferences, communication style
  - feedback: Corrections to agent behavior
  - project: Ongoing work, architecture decisions, business context
  - reference: External resources, API docs, links
- content: The actual memory content (max 2000 chars)
"""

LIST_MEMORIES_DESCRIPTION = """List all saved memories with their names, types, and descriptions.
Use this to check what has been remembered before saving duplicates.
Also use when the user asks "what do you remember?" or similar."""

DELETE_MEMORY_DESCRIPTION = """Delete a memory by filename (e.g., "prefers-tailwind.md").
Use when the user asks to forget something, or when a memory is outdated and being replaced."""

HARD_RESTART_RAILS_DESCRIPTION = """Forcefully restart the Rails (LlamaPress) container — a "hard kick" when a soft restart isn't enough.

This stops and restarts the entire Rails container process. Use it when:
- A soft restart (`rm -f tmp/restart.txt && touch tmp/restart.txt` via `bash_command`) didn't pick up the change.
- You changed Gemfile / Gemfile.lock and need bundler to re-resolve.
- You changed an initializer in `config/initializers/` or anything Puma loads at boot.
- You changed env vars in `.env` that Rails reads at boot.
- The Rails process is wedged / unresponsive and needs to be killed.

For ordinary code or routes changes, PREFER the soft restart over this tool — it's ~2s vs ~15-30s, keeps DB connections warm, and is gentler on the user. Reach for hard_restart_rails only when the soft path is insufficient.

What this does NOT do:
- Restart the LlamaBot (your own) container. You'd kill yourself mid-call.
- Re-read `docker-compose.yml`. For that, the user has a `/restart` slash command.

After calling this, the container is briefly unavailable (~10-30s). Tell the user the page will be unreachable for a few seconds and come back automatically. Do not retry `bash_command` immediately after — wait until the container is back.
"""

FIX_PERMISSIONS_DESCRIPTION = """Fix file permission errors in the Rails container by resetting ownership on common problematic directories (tmp/, coverage/, log/).

## WHEN TO USE THIS TOOL (MANDATORY)

Call this tool IMMEDIATELY when you see ANY of these errors:
- "Permission denied" (e.g., sprockets cache, tmp/cache/assets, coverage/)
- "EACCES"
- "Operation not permitted"
- "apply2files" errors from sprockets cache

This is the ONLY correct fix for permission errors. After calling this tool, retry your original command.

## NEVER DO THESE INSTEAD:
- ❌ Do NOT disable sprockets cache in config/environments/test.rb or any environment file
- ❌ Do NOT modify Rails config files to work around permission errors
- ❌ Do NOT run chmod/chown via bash_command — it runs as UID 1000 which cannot fix root-owned files
- ❌ Do NOT try `sudo` — it's not available in the container

These are all anti-patterns. The fix_permissions tool runs as root inside the Rails container and resets ownership. That's the correct solution. It is safe to run multiple times (idempotent).
"""

TAIL_RAILS_LOGS_DESCRIPTION = """Read recent stdout/stderr logs from the Rails container via the Docker logs API.

Works even when the Rails container is STOPPED or CRASHED — Docker keeps the logs around. This is the primary
diagnostic tool when `bash_command` fails with "409 Conflict" or "container not running" — that means the
Rails process exited (usually a bad migration, missing gem, or syntax error on boot), and the logs hold the
real reason.

When to use:
- `bash_command` just returned "409", "container not running", or hangs — read the logs to find out why Rails died.
- A `bundle exec rails db:migrate` or `db:prepare` step failed and you need the actual error message.
- The user reports "the page is broken", a 500 error, or "nothing is loading".
- After a long-running request, to see server-side output (rendered template, query log, exception trace).

Parameters:
- lines: Number of recent log lines to return (default 200, max 2000).

Returns the demultiplexed log text (stdout + stderr interleaved). Does not write a file.
"""

VIEW_CURRENT_PAGE_HTML_DESCRIPTION = """
The `view_page` tool gives you what the user is seeing, and backend context, as ground truth for all UI-related/exploratory questions.

It provides you with the complete context of the page the user is currently viewing — including the fully rendered HTML, the Rails route, the controller, and the view template that produced the page. Use this as your source of truth for answering questions related to the visible UI and its server-side origins.

WHEN TO CALL:
The user's question relates to what they're viewing, the layout, styles, invisible/missing elements, UI bugs, what's visible, or why something looks/is behaving a certain way.
You need to confirm what the user is seeing ("what is this page?", "why is button X missing?", "what template is being rendered?").
Before proposing a fix for any UI, CSS, DOM, or view issue to ensure recommendations are context-aware.
Any time you need ground truth about the user's current page state.

WHEN NOT TO CALL:
For general programming, theory, or framework/library questions unrelated to the current visible page.
When answering backend-only, architectural, or code-only questions not dependent on rendered output.

USAGE RULES:
This is an important tool you will use frequently to understand and "see" what the user is looking at. Use once per user interaction unless the user navigates/reloads to a different page afterwards.
If the HTML is excessively large, use the max_chars parameter to fetch only as much as you need; if further detail is needed, ask the user for a narrower target (specific element, component, selector).
In your explanation, refer to the route, controller, and view path to anchor your advice precisely.
"""
