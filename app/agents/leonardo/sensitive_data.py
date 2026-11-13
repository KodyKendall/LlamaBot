"""Sensitive-data filter for text going to a model on a ZDR box (LP-33, HECVAT AIGN-05).

Built-in rules cover US SSNs, credit cards (Luhn-checked) and a configurable student
ID format. An admin can add regex rules and choose redact or block per rule, through
``PUT /api/sensitive-data-rules``.

Runs from the hook in :mod:`app.agents.leonardo.safety_hooks`, on every model call.

Only the copy sent to the model is changed. The chat history on the box keeps the
original, which stays on the customer's own box.

Logs and reports rule names and counts. Matched text is never logged or raised.

Known limit: images pass through unscanned, since this does not read text out of
pictures. Attached files that cannot be scanned (PDF, video) are withheld.
"""
import hashlib
import json
import logging
import re
import threading
import time
from collections import Counter, OrderedDict
from dataclasses import dataclass
from typing import Optional

from langchain_core.messages import AIMessage, BaseMessage

logger = logging.getLogger(__name__)

SETTING_KEY = "sensitive_data_rules"
REDACT = "redact"
BLOCK = "block"
ACTIONS = (REDACT, BLOCK)

SSN_PATTERN = r"\b\d{3}-\d{2}-\d{4}\b"
CARD_PATTERN = r"\b\d(?:[ -]?\d){12,18}\b"
#: University of Utah uNID. Institutions set their own format in the rules.
DEFAULT_STUDENT_ID_PATTERN = r"\b[uU]\d{7}\b"

BUILTIN_RULES = ("ssn", "credit_card", "student_id")
MAX_PATTERN_LENGTH = 200
MAX_STORED_LENGTH = 1000  # SiteSetting.value is max_length=1000
_NAME = re.compile(r"^[A-Za-z0-9_-]{1,30}$")

CONFIG_TTL_SECONDS = 30
SEEN_LIMIT = 5000

# Blocks that carry bytes this filter cannot read. Withheld rather than sent.
_UNSCANNABLE_BLOCKS = {"file", "document", "input_file", "media", "video", "audio"}
# Dict keys whose values are not message text; changing them breaks the provider call.
_SKIP_KEYS = {"type", "id", "signature", "cache_control", "image_url", "tool_call_id", "name"}

WITHHELD_FILE_NOTE = (
    "[Attached file withheld: this workspace cannot scan it for sensitive data. "
    "If it was uploaded, use the read_uploaded_file tool to read its text.]"
)


class SensitiveDataBlocked(Exception):
    """Raised when a block rule matches new text. Carries the rule, never the match."""


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: "re.Pattern"
    action: str = REDACT
    luhn: bool = False

    @property
    def token(self) -> str:
        return f"[REDACTED: {self.name.upper()}]"


def luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def stored_config() -> dict:
    """The admin's saved rules, or {} when none are saved."""
    from sqlmodel import Session

    from app.db import engine
    from app.models import SiteSetting

    with Session(engine) as session:
        setting = session.get(SiteSetting, SETTING_KEY)
        return json.loads(setting.value) if setting and setting.value else {}


def _check_pattern(pattern) -> None:
    if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
        raise ValueError(f"Patterns must be 1 to {MAX_PATTERN_LENGTH} characters")
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"Invalid pattern: {e}") from e


def validate_config(config) -> dict:
    """Raise ValueError unless ``config`` is safe to store. Returns the cleaned form."""
    if not isinstance(config, dict):
        raise ValueError("Rules must be an object")

    cleaned: dict = {}
    actions = config.get("actions", {})
    if not isinstance(actions, dict):
        raise ValueError("actions must be an object")
    for name, action in actions.items():
        if name not in BUILTIN_RULES:
            raise ValueError(f"Unknown built-in rule: {name}")
        if action not in ACTIONS:
            raise ValueError(f"Action for {name} must be redact or block")
    if actions:
        cleaned["actions"] = actions

    if config.get("student_id_pattern"):
        _check_pattern(config["student_id_pattern"])
        cleaned["student_id_pattern"] = config["student_id_pattern"]

    custom = config.get("custom", [])
    if not isinstance(custom, list):
        raise ValueError("custom must be a list")
    rows = []
    for row in custom:
        if not isinstance(row, dict) or not _NAME.match(str(row.get("name", ""))):
            raise ValueError("Each custom rule needs a name: letters, digits, _ or -, up to 30")
        if row["name"] in BUILTIN_RULES:
            raise ValueError(f"{row['name']} is a built-in rule")
        _check_pattern(row.get("pattern"))
        action = row.get("action", REDACT)
        if action not in ACTIONS:
            raise ValueError(f"Action for {row['name']} must be redact or block")
        rows.append({"name": row["name"], "pattern": row["pattern"], "action": action})
    if rows:
        cleaned["custom"] = rows

    if len(json.dumps(cleaned)) > MAX_STORED_LENGTH:
        raise ValueError("Too many rules to store")
    return cleaned


def _compile(name: str, pattern: str, action: str, luhn: bool = False) -> Optional[Rule]:
    """A stored rule that no longer compiles is skipped, never fatal to the built-ins."""
    if action not in ACTIONS:
        action = REDACT
    try:
        return Rule(name, re.compile(pattern), action, luhn)
    except (re.error, TypeError) as e:
        logger.error("Sensitive data filter: skipping rule %s: %s", name, e)
        return None


def _build_rules(config: dict) -> list:
    actions = config.get("actions") or {}
    student_id = config.get("student_id_pattern") or DEFAULT_STUDENT_ID_PATTERN
    rules = [
        _compile("ssn", SSN_PATTERN, actions.get("ssn", REDACT)),
        _compile("credit_card", CARD_PATTERN, actions.get("credit_card", REDACT), luhn=True),
        _compile("student_id", student_id, actions.get("student_id", REDACT)),
    ]
    for row in config.get("custom") or []:
        if isinstance(row, dict):
            rules.append(_compile(str(row.get("name")), row.get("pattern"), row.get("action", REDACT)))
    return [rule for rule in rules if rule]


_lock = threading.Lock()
_rules_cache: Optional[tuple] = None  # (expires_at, rules)
_counts: Counter = Counter()
_seen: OrderedDict = OrderedDict()


def current_rules() -> list:
    """Cached briefly, because this runs on every model call. Fails safe to the built-ins."""
    global _rules_cache
    now = time.monotonic()
    if _rules_cache and _rules_cache[0] > now:
        return _rules_cache[1]
    try:
        config = stored_config()
        if not isinstance(config, dict):
            config = {}
    except Exception as e:  # noqa: BLE001 - never send unfiltered because the DB hiccuped
        logger.error("Sensitive data filter: using built-in rules, could not read saved ones: %s", e)
        config = {}
    rules = _build_rules(config)
    _rules_cache = (now + CONFIG_TTL_SECONDS, rules)
    return rules


def invalidate_rules() -> None:
    global _rules_cache
    _rules_cache = None


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _scrub(text: str, rules: list, found: Counter) -> str:
    for rule in rules:
        def replace(match, rule=rule):
            if rule.luhn and not luhn_valid(re.sub(r"\D", "", match.group(0))):
                return match.group(0)
            found[rule.name] += 1
            return rule.token

        text = rule.pattern.sub(replace, text)
    return text


def _walk(value, rules: list, found: Counter):
    """Scrub every string in a message's content or tool arguments."""
    if isinstance(value, str):
        return _scrub(value, rules, found)
    if isinstance(value, list):
        return [_walk(item, rules, found) for item in value]
    if isinstance(value, dict):
        if value.get("type") in _UNSCANNABLE_BLOCKS:
            return {"type": "text", "text": WITHHELD_FILE_NOTE}
        return {
            key: item if key in _SKIP_KEYS else _walk(item, rules, found)
            for key, item in value.items()
        }
    return value


def _scan_message(message: BaseMessage, rules: list, found: Counter) -> BaseMessage:
    update = {"content": _walk(message.content, rules, found)}
    if isinstance(message, AIMessage):
        if message.tool_calls:
            update["tool_calls"] = [
                {**call, "args": _walk(call.get("args"), rules, found)} for call in message.tool_calls
            ]
        if message.additional_kwargs:
            update["additional_kwargs"] = _walk(message.additional_kwargs, rules, found)
    return message.model_copy(update=update)


def _record(message: BaseMessage, found: Counter) -> None:
    """Count each message once, since the whole history is rescanned on every call."""
    if not found:
        return
    # In memory only, one-way, and over the whole message rather than the match.
    key = hashlib.sha256(
        f"{message.type}:{message.id}:{message.content}".encode("utf-8", "replace")
    ).hexdigest()
    with _lock:
        if key in _seen:
            return
        _seen[key] = True
        while len(_seen) > SEEN_LIMIT:
            _seen.popitem(last=False)
        _counts.update(found)
    logger.warning("Sensitive data filter: %s", dict(found))


def sanitize_messages(messages: list) -> list:
    """Return copies safe to send to a model.

    A block rule fires only on messages newer than the last model reply. The blocked
    message stays in history, so blocking on old text would wedge the thread for good;
    on later turns it is redacted instead.
    """
    rules = current_rules()
    last_reply = max((i for i, m in enumerate(messages) if isinstance(m, AIMessage)), default=-1)
    blocking = {rule.name for rule in rules if rule.action == BLOCK}

    safe = []
    for index, message in enumerate(messages):
        found: Counter = Counter()
        cleaned = _scan_message(message, rules, found)
        _record(message, found)
        if index > last_reply:
            hit = sorted(blocking & set(found))
            if hit:
                raise SensitiveDataBlocked(", ".join(name.upper() for name in hit))
        safe.append(cleaned)
    return safe


def counts() -> dict:
    with _lock:
        return dict(_counts)


def reset_for_tests() -> None:
    global _rules_cache
    with _lock:
        _rules_cache = None
        _counts.clear()
        _seen.clear()
