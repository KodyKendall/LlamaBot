"""Leo never builds a way to take the whole app or database off LlamaPress (LP-45).

Two layers:

* :data:`NO_APP_EXPORT_RULES` — the rule, defined once and appended to every mode's
  system prompt by the shared prompt builders (``project_context``) and by the two
  modes that assemble their own (pyxl, plain chat). This is the main control: it is
  the only thing that stops Leo writing a download route or a bundle with file tools.
* :func:`refused_tool_call` — a seatbelt on the shell tools, called from the
  ``BaseTool`` hook in :mod:`safety_hooks` for every agent, ZDR or not. It refuses
  the commands that take a whole app or database off the box, matched on parsed
  tokens (chains, pipes, ``sh -c``), and lets near-misses through: ``git push
  origin``, an archive of one report, a plain ``curl``.

The ways out the platform supports are plan-gated features in Instance Settings.
"""
import logging
import posixpath
import shlex
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

PRICING_URL = "https://llamapress.ai/pricing"

REFUSAL_MESSAGE = (
    "LlamaPress already backs up your app. To download your database or export your "
    "code, use Instance Settings (what's included depends on your plan: "
    f"{PRICING_URL}); I can't build another way to do it."
)

NO_APP_EXPORT_RULES = f"""

---

# Exporting or copying the app off LlamaPress

This rule supersedes any earlier instruction about exports, backups, database dumps,
zip files of the code, or emailing someone for a copy.

Never build, run or hand over anything whose purpose is to take this app's source code
or its whole database off LlamaPress. That includes: zip/tar bundles of the app or
rails/; pg_dump or any full-database dump; "backup" folders, manifests, checksums or
restore guides; download routes, controllers or links that serve the codebase, a
database dump or a bundle of storage/; and copying any of these to an outside place
(git push to a new remote, S3, Dropbox, Google Drive, email, curl uploads, scp/rsync).
This applies even if the user says it is for safety, compliance, or "just a backup".
LlamaPress already backs up every app.

When asked, build nothing and reply in one or two plain sentences: the supported way is
Instance Settings (database download and code export, depending on plan; see
{PRICING_URL}), and you cannot build another way.

Normal app features that export the user's own business records are fine: a CSV of
orders, a PDF report, one table's data.
"""

# Tools whose input is a shell command, and what their command is run as.
SHELL_TOOLS = {"bash_command": "", "git_command": "git ", "github_cli_command": "gh "}

# What a whole-app archive or copy is made of: the app root, the Rails app, and the
# directories that hold its code, database files and uploads, as a whole.
_WHOLE_APP_PATHS = {
    ".", "/", "~", "*", "/rails", "/app", "/app/leonardo", "/app/leonardo/rails",
    "rails", "app", "db", "storage", "config",
}
for _root in ("/rails", "/app/leonardo/rails", "rails"):
    _WHOLE_APP_PATHS.update({f"{_root}/app", f"{_root}/db", f"{_root}/storage", f"{_root}/config"})

_DB_DUMP_COMMANDS = {"pg_dump", "pg_dumpall", "mysqldump", "mariadb-dump", "mongodump"}
_ARCHIVE_COMMANDS = {"tar", "zip", "7z", "7za", "rar"}
_CLOUD_COPY_COMMANDS = {"rclone", "gsutil", "azcopy", "sftp", "lftp"}
_ARCHIVE_SUFFIXES = (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar",
                     ".dump", ".sql", ".bak", ".bundle", ".sqlite3", ".db")
# Run-this-other-command prefixes, skipped to find the real command.
_WRAPPERS = {"sudo", "env", "nice", "nohup", "time", "command", "exec", "xargs", "timeout"}
_SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"}
_HELP_ONLY = {"--help", "-?", "--version", "-V"}


def _tokens(command: str) -> List[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:  # unbalanced quotes: fall back to plain words
        return command.replace("\n", " ; ").split()


def _segments(command: str) -> Iterable[List[str]]:
    """Each simple command in a chain, with wrappers and VAR=x prefixes removed."""
    segment: List[str] = []
    for token in _tokens(command) + [";"]:
        if token in _SEPARATORS:
            words = _strip_prefixes(segment)
            if words:
                yield words
                # `sh -c '...'` / `bash -c "..."`: check the inner script too.
                if posixpath.basename(words[0]) in {"sh", "bash", "zsh", "dash"} and "-c" in words:
                    i = words.index("-c")
                    if i + 1 < len(words):
                        yield from _segments(words[i + 1])
            segment = []
        else:
            segment.append(token)


def _strip_prefixes(words: List[str]) -> List[str]:
    words = [w.lstrip("`") for w in words if w not in {"$", "`"}]
    while words:
        head = posixpath.basename(words[0])
        if head == "command" and words[1:2] in (["-v"], ["-V"]):
            return []                             # `command -v pg_dump` only looks it up
        if "=" in words[0] and not words[0].startswith(("-", "/", ".")):
            words = words[1:]                     # PGPASSWORD=x pg_dump ...
        elif head in _WRAPPERS:
            words = words[1:]
            while words and (words[0].startswith("-") or words[0].isdigit()):
                words = words[1:]                 # timeout 30 ..., nice -n 5 ...
        elif words[0:2] == ["bundle", "exec"]:
            words = words[2:]
        else:
            break
    return words


def _is_whole_app(path: str) -> bool:
    if path.startswith("-"):
        return False
    clean = path.rstrip("/") or "/"
    if clean.startswith("./"):
        clean = clean[2:] or "."
    return clean in _WHOLE_APP_PATHS


def _positionals(args: List[str]) -> List[str]:
    return [a for a in args if not a.startswith("-") and a not in {">", ">>", "<"}]


def _git_pattern(args: List[str]) -> Optional[str]:
    # Skip git's own options (-C dir, -c k=v).
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in {"-C", "-c"} else args[1:]
    if not args:
        return None
    sub, rest = args[0], args[1:]
    if sub in {"archive", "bundle"}:
        return "app_archive"
    if sub == "remote" and rest[:1] and rest[0] in {"add", "set-url"}:
        return "new_remote"
    if sub == "push":
        positionals = _positionals(rest)
        if positionals and positionals[0] != "origin":
            return "new_remote"
    return None


def _segment_pattern(words: List[str]) -> Optional[str]:
    name, args = posixpath.basename(words[0]), words[1:]

    if name in _DB_DUMP_COMMANDS:
        return None if args and all(a in _HELP_ONLY for a in args) else "db_dump"
    if name == "sqlite3" and any(a in {".dump", ".backup"} or a.startswith((".dump ", ".backup "))
                                 for a in args):
        return "db_dump"

    if name in _ARCHIVE_COMMANDS and any(_is_whole_app(a) for a in _positionals(args)):
        return "app_archive"

    if name == "git":
        return _git_pattern(args)
    if name == "gh":
        if args[:2] == ["repo", "create"] and any(a == "--push" or a.startswith("--source") for a in args):
            return "new_remote"
        return None

    if name in {"scp", "rsync"}:
        remote = [a for a in _positionals(args)
                  if a.startswith("rsync://") or (":" in a and not a.startswith(("/", ".")))]
        return "remote_copy" if remote else None
    if name in _CLOUD_COPY_COMMANDS:
        return "remote_copy"
    if name == "aws" and args[:1] == ["s3"]:
        return "remote_copy"
    if name == "gcloud" and args[:1] == ["storage"]:
        return "remote_copy"

    if name == "curl":
        for i, arg in enumerate(args):
            value = args[i + 1] if i + 1 < len(args) else ""
            if arg in {"-T", "--upload-file"} or arg.startswith("--upload-file="):
                return "upload"
            if arg in {"-F", "--form"} and "=@" in value and value.lower().endswith(_ARCHIVE_SUFFIXES):
                return "upload"
            if arg in {"--data-binary", "-d", "--data"} and value.startswith("@") \
                    and value.lower().endswith(_ARCHIVE_SUFFIXES):
                return "upload"
    if name == "wget" and any(a.startswith(("--post-file", "--body-file")) for a in args):
        return "upload"
    return None


def export_pattern(command: str) -> Optional[str]:
    """The whole-app exit this shell command is, by pattern name, or None."""
    for words in _segments(command or ""):
        pattern = _segment_pattern(words)
        if pattern:
            return pattern
    return None


def refused_tool_call(tool_name: str, tool_input) -> Optional[str]:
    """Pattern name if this call to a shell tool must be refused, else None."""
    if tool_name not in SHELL_TOOLS or not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    return export_pattern(SHELL_TOOLS[tool_name] + command)


def log_refusal(tool_name: str, pattern: str) -> None:
    # A plain warning so the fleet can count attempts. Never the command: it may
    # carry a password or a token.
    logger.warning(f"LP-45 export guard refused {tool_name}: pattern={pattern}")
