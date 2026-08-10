"""The instance's ``.env``: a tiny write-only-ish surface, deliberately.

**Nothing in this module ever returns the contents of the .env file.** Not
values, not key names, not a "is it set" bit. There is no reveal path, and the
API layer has no endpoint that could grow one. The file holds every provider API
key, the database URLs, the ``LLAMAPRESS_AI_LOGIN_SECRET`` and the VS Code
password (shell access to the box) — so the safe design is for the browser to
never learn anything about it.

Exactly two things are exposed:

  1. :data:`KNOWN_TOGGLES` — a hard-coded allowlist of **boolean** operator gates.
     :func:`set_toggle` coerces its argument to the literal ``"true"``/``"false"``
     and refuses any key outside the list, so this path cannot write arbitrary
     text, and cannot write to any key that isn't on it.
  2. Custom variables the user adds themselves, rendered into a delimited managed
     block. They can never override anything already in the file.

This is an allowlist, not a denylist. A key added to the file later is invisible
and untouchable by default; making it visible or editable takes a deliberate code
change here. (The previous shape — "block a list of protected keys, expose the
rest" — fails open the moment someone adds a secret nobody thought to list.)

**Which .env.** There are up to three on a running box and only one is real:

  * ``/app/app/.env`` — container CWD, what bare ``load_dotenv()`` reads. Near
    empty, and in the container's *ephemeral* layer, so writes vanish on the next
    ``docker compose up -d``. This is the trap that regenerated ``SESSION_SECRET``
    on every recreate and logged the fleet out (``token_service``).
  * ``/app/leonardo/.env`` — the **host** file, mounted via Leonardo's
    ``./:/app/leonardo`` and loaded by BOTH services (``env_file: .env`` on
    ``llamapress`` and ``llamabot``). Gitignored, and off ``bin/update``'s sync
    ALLOWLIST, so it survives updates. This is the one we manage.
  * The process environment, which compose populated from that host file at start.

**Why an edit needs a restart.** Values reach the process through compose's
``env_file`` at container start, not by reading the file at runtime — rewriting
the file changes nothing live. Both toggles here are re-read from ``os.environ``
per call, so :func:`set_toggle` also updates ``os.environ`` and they apply
immediately. Custom variables are consumed by the Rails container, which can only
pick them up on a recreate, so those always report pending.
"""

import logging
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

logger = logging.getLogger(__name__)

MANAGED_BEGIN = "# >>> llamabot managed: custom variables (edit in Settings, not by hand) >>>"
MANAGED_END = "# <<< llamabot managed <<<"

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
MAX_NAME_LEN = 128
MAX_VALUE_LEN = 4096

#: What the UI shows in place of any value, always. There is no code path that
#: substitutes a real value for this.
MASK = "••••••••"

_TRUTHY = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Toggle:
    """A boolean operator gate the user is allowed to flip."""

    key: str
    label: str
    description: str
    default: bool


#: THE allowlist. Adding an entry here is the only way to make a variable
#: user-editable, and it can still only ever be set to "true" or "false".
#:
#: Deliberately excluded, with reasons, so nobody re-adds them casually:
#:   PAYWALL_ENABLED       — revenue gate; the user could switch off their own paywall.
#:   WS_AUTH_REQUIRED      — off means an unauthenticated chat websocket.
#:   LLAMABOT_COOKIE_SECURE— false downgrades session cookies to plain HTTP.
#:   LLAMABOT_ENABLE_FAKE_LLM — test-only; would silently stub out the model.
#:   ENABLE_GITHUB_BUTTON  — operator's call, not the instance user's.
#: Anything holding a credential is excluded by construction: this list is
#: boolean-only and :func:`set_toggle` writes nothing but "true"/"false".
KNOWN_TOGGLES: tuple = (
    Toggle(
        key="MODEL_SWITCHING_ALLOWED",
        label="Model dropdown",
        description=(
            "Let users pick the model in chat. When off, the dropdown is hidden "
            "and every turn uses the default model."
        ),
        default=False,
    ),
    Toggle(
        key="VISION_MODEL_ALLOWED",
        label="Image attachments",
        description=(
            "Allow images to be attached and sent to a vision model. When off, "
            "attachments are refused at pick time and stripped server-side."
        ),
        default=False,
    ),
)

TOGGLE_KEYS = frozenset(t.key for t in KNOWN_TOGGLES)

#: Names a custom variable may never take, independent of what this box happens
#: to have configured. Kept static so a rejection reveals a platform fact rather
#: than the contents of this instance's file.
RESERVED_PREFIXES = (
    "LLAMABOT_", "LLAMAPRESS_", "LEONARDO_", "RAILS_", "POSTGRES_", "REDIS_",
    "AWS_", "CHATGPT_", "WS_", "SESSION_", "SECRET_", "DB_", "AUTH_",
    "CHECKPOINT", "SCHEDULER_", "VSCODE_", "GITHUB_", "CODEX_",
)
RESERVED_SUFFIXES = ("_API_KEY", "_SECRET", "_PASSWORD", "_TOKEN", "_KEY", "_URI")
RESERVED_EXACT = frozenset({
    "DATABASE_URL", "ENABLED_MODELS", "DISABLED_MODELS", "PAYWALL_ENABLED",
    "INSTANCE_NAME", "LOG_LEVEL", "ENV", "PATH", "HOME", "USER", "SHELL",
    "COOKBOOK_URL", "S3_BUCKET_PATH",
})


class EnvValidationError(ValueError):
    """Raised for a rejected name or value. Message is safe to show the user."""


# --------------------------------------------------------------------------
# Locating the file
# --------------------------------------------------------------------------

def _candidate_paths() -> list:
    """Ordered candidates for the durable host ``.env``.

    The bare-CWD ``.env`` is deliberately NOT a candidate — see the module
    docstring.
    """
    candidates = []
    explicit = os.environ.get("LEONARDO_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    for var in ("LEONARDO_PATH", "HOST_LEONARDO_PATH"):
        base = os.environ.get(var, "").strip()
        if base:
            candidates.append(Path(base) / ".env")
    candidates.append(Path("/app/leonardo/.env"))
    return candidates


def env_file_path() -> Optional[Path]:
    """The durable ``.env`` we manage, or ``None`` when there isn't one.

    ``None`` is a supported state: a differently-laid-out box (or CI) gets a
    read-only panel rather than a traceback.
    """
    for path in _candidate_paths():
        try:
            if path.is_file():
                return path
        except OSError:
            continue
    return None


def is_writable() -> bool:
    """True when the managed file exists and this process can rewrite it."""
    path = env_file_path()
    if path is None:
        return False
    return os.access(path, os.W_OK) and os.access(path.parent, os.W_OK)


# --------------------------------------------------------------------------
# Parsing (internal only — no parsed content is ever returned to a caller)
# --------------------------------------------------------------------------

def _unescape_double_quoted(value: str) -> str:
    """Undo :func:`format_assignment`'s escaping (``\\\\`` and ``\\"`` only)."""
    out, i = [], 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value) and value[i + 1] in ('"', "\\"):
            out.append(value[i + 1])
            i += 2
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _unquote(raw: str) -> str:
    """Strip one layer of matching quotes, as compose does.

    Escapes are processed only inside DOUBLE quotes, matching compose's rule.
    """
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        inner = value[1:-1]
        return _unescape_double_quoted(inner) if value[0] == '"' else inner
    return value


def parse_line(line: str) -> Optional[tuple]:
    """``(key, value)`` for an assignment line, else ``None``.

    Comments, blanks and malformed lines are ignored rather than rejected — the
    file is hand-edited by operators and we are a guest in it.
    """
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    if "=" not in stripped:
        return None
    key, _, raw = stripped.partition("=")
    key = key.strip()
    if not NAME_RE.match(key):
        return None
    return key, _unquote(raw)


def split_managed(lines: Iterable) -> tuple:
    """Split file lines into ``(before, managed_body, after)``, delimiters dropped."""
    before, managed, after = [], [], []
    state = "before"
    for line in lines:
        stripped = line.strip()
        if state == "before" and stripped == MANAGED_BEGIN:
            state = "managed"
            continue
        if state == "managed" and stripped == MANAGED_END:
            state = "after"
            continue
        if state == "before":
            before.append(line)
        elif state == "managed":
            managed.append(line)
        else:
            after.append(line)
    if state == "managed":
        logger.warning("Unterminated llamabot managed block in .env; will be rewritten")
    return before, managed, after


def read_lines() -> list:
    """Raw lines of the managed file, or ``[]``. Internal use only."""
    path = env_file_path()
    if path is None:
        return []
    try:
        return path.read_text().splitlines()
    except OSError as e:
        logger.warning("Could not read %s: %s", path, e)
        return []


def base_keys(lines: Optional[Iterable] = None) -> set:
    """Keys defined OUTSIDE the managed block.

    Used only for collision detection. **Never return this to a client** — it is
    the contents of the file, which is exactly what this module does not disclose.
    """
    if lines is None:
        lines = read_lines()
    before, _managed, after = split_managed(lines)
    keys = set()
    for line in list(before) + list(after):
        parsed = parse_line(line)
        if parsed:
            keys.add(parsed[0])
    return keys


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate_name(name: str) -> str:
    """Return the cleaned name, or raise :class:`EnvValidationError`."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise EnvValidationError("Variable name is required")
    if len(cleaned) > MAX_NAME_LEN:
        raise EnvValidationError(f"Variable name must be {MAX_NAME_LEN} characters or fewer")
    if not NAME_RE.match(cleaned):
        raise EnvValidationError(
            "Variable name must be UPPERCASE letters, digits and underscores, "
            "starting with a letter (e.g. MY_SERVICE_URL)"
        )
    return cleaned


def validate_value(value: str) -> str:
    """Return the value, or raise :class:`EnvValidationError`.

    Newlines are refused outright: a ``\\n`` inside a value would terminate the
    assignment and inject whatever follows as a SECOND variable — which is how a
    custom-variable field turns into a way to set a platform key.
    """
    if value is None:
        raise EnvValidationError("Value is required")
    text = str(value)
    if len(text) > MAX_VALUE_LEN:
        raise EnvValidationError(f"Value must be {MAX_VALUE_LEN} characters or fewer")
    if "\n" in text or "\r" in text:
        raise EnvValidationError("Value cannot contain line breaks")
    if "\x00" in text:
        raise EnvValidationError("Value cannot contain null bytes")
    return text


def is_reserved(name: str) -> bool:
    """True when ``name`` may not be used for a custom variable.

    Covers the static platform namespace AND whatever this instance already has
    configured — the latter because a colliding custom variable would be silently
    dropped at render time, which is worse than being told no.
    """
    if name in RESERVED_EXACT or name in TOGGLE_KEYS:
        return True
    if name.startswith(RESERVED_PREFIXES) or name.endswith(RESERVED_SUFFIXES):
        return True
    return name in base_keys()


#: One message for every rejection, so it cannot be used to probe whether a
#: particular variable is configured on this instance.
RESERVED_MESSAGE = (
    "That name is reserved or already in use. Pick a different name — custom "
    "variables can never replace an existing setting."
)


# --------------------------------------------------------------------------
# Rendering and writing
# --------------------------------------------------------------------------

def format_assignment(key: str, value: str) -> str:
    """One ``KEY="value"`` line, quoted so spaces and ``#`` survive."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{key}="{escaped}"'


def render_managed_block(custom_vars: dict, existing: set) -> list:
    """Lines for the managed block, minus anything that would shadow ``existing``.

    Compose takes the LAST occurrence of a duplicate key and this block sits at
    the end of the file, so an unfiltered render would silently override the
    operator's value. This filter — not the save-time check — is the guard that
    actually holds.
    """
    body = [MANAGED_BEGIN]
    for key in sorted(custom_vars):
        if key in existing:
            logger.info(
                "Custom variable %s is shadowed by an existing .env entry; not rendered", key
            )
            continue
        body.append(format_assignment(key, custom_vars[key]))
    body.append(MANAGED_END)
    return body


#: Where .env backups go. Deliberately NOT next to the .env file.
#:
#: Leonardo's compose mounts the whole project into the code-server container
#: (``./:/config/workspace``), so anything written beside ``.env`` is readable
#: from the customer's editor terminal. The 2026-08-09 fleet audit found the raw
#: ``.env`` exposed that way on 96 of 97 boxes, and an old ``.env.bak`` riding
#: the same mount. Writing timestamped backups there would have added a fresh
#: copy of every secret on each toggle flip. This directory is inside the
#: container only, so a backup never reaches the host or the editor.
_BACKUP_DIR = Path("/tmp/llamabot-env-backups")


def _backup(path: Path, keep: int = 5) -> None:
    """Copy ``path`` into the container-local backup directory.

    Backups exist to recover from a bad edit within a session. They are
    intentionally NOT durable: a container recreate discards them, which is the
    right trade against leaving secret copies on a mounted volume.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    try:
        _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(_BACKUP_DIR, 0o700)
        backup = _BACKUP_DIR / f"{path.name}.bak-{stamp}"
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    except OSError as e:
        logger.warning("Could not back up %s: %s", path, e)
        return
    try:
        for stale in sorted(_BACKUP_DIR.glob(f"{path.name}.bak-*"))[:-keep]:
            stale.unlink()
    except OSError as e:
        logger.warning("Could not prune .env backups: %s", e)


def _restore_ownership(target: Path, original_stat) -> None:
    """Give ``target`` the uid/gid recorded in ``original_stat``.

    Best effort: when this process is not root it cannot chown, and in that case
    the owner already matches (a non-root process could only have written a file
    it owns). Never raises — a failed chown must not lose the edit.
    """
    try:
        if os.getuid() == original_stat.st_uid:
            return
        os.chown(target, original_stat.st_uid, original_stat.st_gid)
    except (OSError, AttributeError) as e:
        logger.warning(
            "Could not restore .env ownership to %s:%s — the host operator may be "
            "unable to edit it: %s", original_stat.st_uid, original_stat.st_gid, e
        )


def write_lines(lines: list) -> None:
    """Atomically replace the managed file's contents.

    Writes a sibling temp file and renames over the target, so a crash or a full
    disk leaves the original intact rather than a truncated .env that would
    refuse to boot the stack.

    The replacement inherits the ORIGINAL file's owner and mode, not the writing
    process's. This container runs as root while the host file belongs to uid
    1000, so a plain rename hands the host a root-owned ``.env`` that the human
    operator and ``bin/update``'s ``set_env_var`` can no longer write — the same
    silent breakage as the root-owned checkpoint Restore bug.
    """
    path = env_file_path()
    if path is None:
        raise EnvValidationError("No writable .env file was found on this instance")

    _backup(path)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    text = "\n".join(lines).rstrip("\n") + "\n"
    try:
        original = path.stat()
        tmp.write_text(text)
        shutil.copymode(path, tmp)
        _restore_ownership(tmp, original)
        os.replace(tmp, path)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise EnvValidationError(f"Could not write .env: {e}") from e


def set_toggle(key: str, enabled) -> bool:
    """Set one allowlisted boolean gate. Returns True if a restart is needed.

    The only write path to a non-custom variable. Two properties make it safe to
    expose: ``key`` must be in :data:`TOGGLE_KEYS`, and the written value is
    *coerced* to the literal ``"true"``/``"false"`` rather than passed through —
    so no caller-supplied text ever reaches the file.
    """
    if key not in TOGGLE_KEYS:
        raise EnvValidationError(f"{key} is not a user-configurable setting")

    value = "true" if enabled in (True, "true", "True", "1", "on", "yes") else "false"

    lines = read_lines()
    before, managed, after = split_managed(lines)

    replaced = False
    for section in (before, after):
        for i, line in enumerate(section):
            parsed = parse_line(line)
            if parsed and parsed[0] == key:
                section[i] = format_assignment(key, value)
                replaced = True
                break
        if replaced:
            break
    if not replaced:
        before.append(format_assignment(key, value))

    rebuilt = list(before)
    if managed or MANAGED_BEGIN in "\n".join(lines):
        rebuilt += [MANAGED_BEGIN] + list(managed) + [MANAGED_END]
    rebuilt += list(after)
    write_lines(rebuilt)

    # Both toggles are read from os.environ per call, so this applies now.
    os.environ[key] = value
    return False


def sync_managed_block(custom_vars: dict) -> None:
    """Rewrite the managed block from ``custom_vars`` (the DB is the source)."""
    lines = read_lines()
    before, _managed, after = split_managed(lines)
    existing = base_keys(lines)
    block = render_managed_block(custom_vars, existing)
    write_lines(list(before) + block + list(after))


# --------------------------------------------------------------------------
# Read model for the UI (toggles only — never file contents)
# --------------------------------------------------------------------------

def env_bool(key: str, default: bool) -> bool:
    """Read a boolean gate the same way ``model_policy._env_bool`` does."""
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in _TRUTHY


def toggle_states() -> list:
    """Current state of each allowlisted switch. The only .env-derived data
    this module will hand to a caller — and every field is a boolean or a
    hard-coded string, never anything read out of the file."""
    return [
        {
            "key": t.key,
            "label": t.label,
            "description": t.description,
            "enabled": env_bool(t.key, t.default),
        }
        for t in KNOWN_TOGGLES
    ]
