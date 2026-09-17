"""Operator/mothership gate on which LLM models an instance may use.

This is distinct from the per-model API-key check (a model with no key is merely
*unconfigured*). Here a model can be fully present — key set, `get_llm` branch
exists — yet deliberately **disabled**, so the instance user cannot select it and
cannot re-enable it. The policy is sourced only from operator-controlled channels
the user has no write path to:

  * The mothership-provisioned instance config, ``.leonardo/instance.json``, keys
    ``enabled_models`` and ``disabled_models`` (JSON arrays of frontend model
    names). Authoritative: mothership owns this file.
  * The ``ENABLED_MODELS`` / ``DISABLED_MODELS`` environment variables
    (comma-separated), baked into the container by the box operator.
  * The OpenRouter model registry (``.leonardo/openrouter_models.json``, see
    :mod:`app.agents.leonardo.openrouter_models`). Also operator-owned and
    host-mounted; registering an entry there enables it (step 2b below).
  * The mothership's pushed policy document (``.leonardo/model_policy.json``, see
    :mod:`app.services.model_policy_store`), delivered on the lease tick. Carries
    ``default_model``, ``enabled_models``, ``disabled_models``, ``roles`` and a
    per-box ``instance_overrides`` block.

**Roles (0.7.7).** ``roles`` maps a role — ``chat`` or ``vision`` — to an ORDERED
fallback chain instead of a single name, and :func:`resolve_role` takes the first
entry this box can actually serve. This is what makes a provider withdrawal a
degraded answer rather than a dead turn: on 2026-08-31 Meta answered 404 for
``muse-spark-1.2-contributor`` for 3h22m, 87 turns died across 14 boxes, and
nothing in the config could route around it because the choice was compiled in.
An explicit ``roles.chat`` outranks ``default_model``, which is kept as the
back-compat spelling. ``roles.vision`` replaces the compiled ``_VISION_MODELS``
floor. :func:`policy_report` reports what actually won, over HTTP, so the
resolved routing is readable without SSH.

**Resolution (most-specific wins):**

  1. **Explicit disable** — a model named in ``disabled_models`` /
     ``DISABLED_MODELS`` is OFF, overriding everything below (including the
     fail-open defaults). This is the deliberate "turn off even a default" knob.
     Disable sources UNION: any source can turn a model off.
  1a. **Model-switching lock** — when ``MODEL_SWITCHING_ALLOWED`` is off (it is
     ON by default since 0.7.0; the var is a per-box opt-OUT), only the resolved
     default text model is enabled, plus the box's resolved vision model when
     ``VISION_MODEL_ALLOWED`` is on (so the image auto-switch still works). This
     coarse operator gate sits above the fail-open/allow-list logic below but
     still yields to an explicit disable in step 1.
  2. **Fail-open defaults** — ``muse-spark-1.2-contributor`` (the fleet default,
     also the image auto-switch target) and ``deepseek-v4-flash`` (what the
     default degrades to on a box with no META key) are globally enabled, so
     every instance always keeps a model it can actually run. Only an explicit
     disable (step 1) turns them off.
  2a. **Resolved vision model** — whichever vision model this box holds a key for
     (see :func:`vision_model`) is enabled while ``VISION_MODEL_ALLOWED`` is on,
     so image uploads work without the operator hand-editing an allow-list on
     every box. Yields to an explicit disable in step 1.
  2b. **Registered OpenRouter models** — an entry in the OpenRouter registry is
     enabled by that registration. The file is operator-owned and host-mounted,
     exactly like the two sources above, so requiring the operator to ALSO name
     it in ``ENABLED_MODELS`` would be configuring one intent twice — the
     friction that registry exists to remove. Yields to an explicit disable in
     step 1, and to ``"enabled": false`` in the entry itself.
  2c. **A configured chat chain** — on a box that names no allow-list, every
     entry of ``roles.chat`` is enabled along with the resolved default. An
     operator who wrote an ordered chain has already said those models are
     acceptable; without this, rung 2 could not use the very entries the chain
     exists to name.
  3. **Allow-list** — if ``enabled_models`` / ``ENABLED_MODELS`` is configured,
     only the named models are enabled (for everything not covered above). Allow
     sources INTERSECT: neither can broaden what the other restricts.
  4. **Default allow-list** — a box that configures no allow-list gets the
     compiled two-model set, NOT "everything that happens to have a key". Fleet
     boxes carry OpenAI/Google/Anthropic keys for other subsystems, and since
     switching now defaults ON, the old inert behavior would have published every
     one of those models to the dropdown.

**Where it is enforced.** The real chokepoint is :func:`get_llm` (in
``llm_factory``): a disabled requested model is replaced with an enabled one before
a client is built. The ``/api/available-models`` endpoint reflects the same rule so
the dropdown greys out disabled models — but that is UX only. Because the websocket
``llm_model`` field is unvalidated user input, gating the dropdown alone would be
bypassable; get_llm is the authoritative gate.
"""

import json
import logging
import os
from typing import Optional

from app.agents.leonardo import model_health
from app.agents.leonardo.llm_factory import (
    _CHATGPT_SUBSCRIPTION_MODELS,
    DEFAULT_LLM_MODEL,
    FALLBACK_TEXT_MODEL,
    fallback_text_model,
    has_provider_key,
)
from app.agents.leonardo.openrouter_models import (
    API_KEY_ENV as OPENROUTER_API_KEY_ENV,
    get_openrouter_model,
    is_openrouter_model,
    openrouter_models,
)

logger = logging.getLogger(__name__)

_INSTANCE_CONFIG_PATH = ".leonardo/instance.json"

# --- Coarse operator gates (env-only, no user write path) --------------------
#
# Two admin toggles that sit ABOVE the per-model allow/disable lists:
#
#   * ``MODEL_SWITCHING_ALLOWED`` — when off, the instance is pinned to the
#     default text model (users can't pick another; the frontend hides the model
#     dropdown). The vision model stays reachable for the image auto-switch ONLY
#     when ``VISION_MODEL_ALLOWED`` is also on.
#   * ``VISION_MODEL_ALLOWED`` — when off, image/video attachments are refused
#     (frontend blocks the send with a support message; the backend also strips
#     them in ``_build_message_content`` as the authoritative gate).
#
# Both are read from the environment only — the box operator controls them, the
# instance user has no write path, exactly like ENABLED_MODELS/DISABLED_MODELS.
# Flip these two defaults to change fleet-wide behavior for instances that never
# set the vars.
_MODEL_SWITCHING_ALLOWED_DEFAULT = True
_VISION_ALLOWED_DEFAULT = False

# The COMPILED FLOOR for the vision role, in preference order — what this box
# resolves to when no operator has said otherwise. Since 0.7.7 it is a floor and
# not the answer: `roles.vision` in a pushed policy replaces it outright, which
# is what makes a bad vision model a config edit instead of a release. See
# :func:`role_chain`.
#
# Whichever entry this box holds a key for is kept reachable (when vision is
# allowed) even while manual switching is locked, so image sends still work
# without opening up the whole dropdown.
#
# Order, and why it moved in 0.7.7:
#
#   * GLM first — it is the fleet default text model as of 2026-08-31 and it is
#     multimodal (MEASURED, not read off a model card: see openrouter_models).
#     Putting the default first means the common box has nothing to switch TO,
#     and the auto-switch only fires for a user who manually moved to a
#     text-only model.
#   * Muse second, for the same reason it was first before: also multimodal,
#     also a general-purpose default, and still the answer on a META-keyed box
#     that has no OpenRouter key. It was NOT retired — the 2026-08-31 404s were
#     a 3h22m upstream blip that recovered — so demoting it is about following
#     the default, not about routing around a dead model.
#   * DeepSeek's vision sibling last (0.7.5), which runs on the DEEPSEEK_API_KEY
#     every box already has. It is the floor under the floor: a box with no
#     OpenRouter and no META key still gets images.
_VISION_MODELS = (
    "glm-5.3-flash-zai",
    "muse-spark-1.2-contributor",
    "deepseek-v4-flash-vision-exp",
)

# The subset of the above that is ONLY a vision target. Muse is absent on
# purpose: it is a general-purpose model that happens to be multimodal, and is
# the fleet's default text model. DeepSeek's vision sibling is not — it is a
# variant you switch TO for an image and back from, so it must never be picked
# as a box's default text model (see enabled_default_model).
_VISION_ONLY_MODELS = frozenset({"deepseek-v4-flash-vision-exp"})

# Preserved as the *preferred* vision model. Prefer `vision_model()` — this
# constant is the head of the compiled floor, which is neither what a box with a
# pushed `roles.vision` runs nor what a box missing that key can build.
VISION_MODEL = _VISION_MODELS[0]


def vision_model() -> str:
    """The vision model THIS box can actually build, or "" if it has none.

    Same shape as :func:`default_text_model` and for the same reason: naming a
    model whose key the box does not hold is not a degraded box, it is a box
    where every image send 401s. Returning "" is meaningful — it is what makes
    the frontend show "no image-capable model is configured" instead of sending
    an image somewhere it cannot be read.

    Since 0.7.7 this walks the ``vision`` role chain (:func:`role_chain`), so a
    pushed ``roles.vision`` overrides the compiled floor and an explicitly
    DISABLED model is routed around. Both are new, and the second one is a bug
    fix, not a refinement: during the 2026-08-31 incident
    ``DISABLED_MODELS=muse-spark-1.2-contributor`` was swept into .env fleet-wide
    and this function went right on returning Muse, because its docstring used to
    say policy "is NOT consulted here". Every image send on those boxes kept
    going to the model the operator had just banned.

    Allow-lists and the switching lock are still NOT consulted — those decide
    what a *user* may pick, and step 2a of :func:`is_model_enabled` exists to
    open whatever this resolves to. Only the disable list, which is the operator
    saying "not this one, anywhere", takes part in routing.
    """
    return resolve_role("vision")


def policy_roles() -> dict:
    """Role chains configured for this box. ``{}`` when nobody configured any.

    Precedence matches :func:`configured_default_model` — **mothership (with any
    per-instance override already merged) > instance.json** — for the same
    reason: the mothership is the operator of record on a fleet box, and a stale
    hand-edit from the last incident must not make the next remote fix a silent
    no-op.

    There is deliberately no environment spelling. ``ENABLED_MODELS``-style CSV
    cannot express an ordered map without inventing a syntax, and the env layer
    is the one place an operator edit does NOT reach a box without a restart —
    which is the property (AGENT_POLICY P-F1b) this whole feature exists to give
    the model choice.
    """
    remote = remote_policy().get("roles")
    if remote:
        return remote

    config = _read_instance_config() or {}
    chains = _clean_role_chains(config.get("roles")) if config.get("roles") else None
    return chains or {}


def role_chain(role: str) -> list:
    """The ordered list of candidates for ``role``, most-preferred first.

    A configured chain REPLACES the compiled floor rather than extending it: an
    operator naming an order and then silently inheriting three more entries
    behind it is not an order, and during an incident the surprising extra entry
    is exactly the one that gets picked.

    ``chat`` has no compiled floor on purpose. Its floor is the whole existing
    resolution path (``default_model`` -> DEFAULT_LLM_MODEL -> fallback_text_model),
    which is richer than a list and which 0.7.6 boxes already depend on; returning
    ``[]`` here means "nobody configured a chain, resolve exactly as before".
    """
    configured = policy_roles().get(role)
    if configured:
        return list(configured)
    if role == "vision":
        return list(_VISION_MODELS)
    return []


def resolve_role(role: str) -> str:
    """The first entry in ``role``'s chain this box can actually serve, or "".

    Four ways an entry is passed over, and each is a real failure someone hit:

      * **not a model this build knows** — a typo, or a name from a newer
        mothership than this box's image. ``has_provider_key`` answers True for
        an unrecognised name (it has no key requirement to check), so without
        this check a misspelled first choice would resolve and then fail at
        ``get_llm``.
      * **no provider key** — the box would 401 on every turn. This is the case
        the chain exists for: entry 1 is an intent, not a guarantee.
      * **explicitly disabled** — an operator ban is a routing fact, not just a
        dropdown fact. See :func:`vision_model` for what happens when it is not.
      * **reporting itself retired** — the ``model_health`` record 0.7.6 added,
        so a 404'd model is skipped for the 15 minutes the record lives instead
        of being re-discovered (and re-paid for) on every turn.

    Returns "" when nothing survives, which hands the decision back to the normal
    walk rather than naming a model the box cannot run. That is the invariant
    that outranks the operator's intent, and it is what keeps a bad remote value
    from being more damaging than the outage it was pushed to fix.
    """
    disabled = _disabled_set()
    known = set(known_models())
    for name in role_chain(role):
        if name not in known:
            logger.warning(
                "Model policy: %s chain names %r, which this build does not know; "
                "skipping it.", role, name,
            )
            continue
        if name in disabled:
            continue
        if not has_provider_key(name):
            continue
        if model_health.is_gone(name):
            continue
        return name
    return ""


# Always enabled regardless of any allow-list, so every instance keeps a model it
# can actually run: the fleet default (Muse, also the image auto-switch target)
# and the text model it degrades to when the box has no META key. These can still
# be turned off, but ONLY via an explicit disable override (see step 1 above); an
# allow-list that omits them does not disable them.
_FAIL_OPEN_MODELS = frozenset({"muse-spark-1.2-contributor", "deepseek-v4-flash"})

# The compiled default enabled set (0.7.0): the two blessed models a box can run
# on operator credentials, PLUS the ChatGPT-subscription entries. Those two are
# not "a model whose key happens to be in .env" — the rule this set exists to
# enforce — because no operator key reaches them: they light up only when a user
# connects their own ChatGPT account, and stay greyed out with "Connect your
# ChatGPT account" until one does. Leaving them out would have made the feature
# unreachable on every fleet box.
# Overridden per box by instance.json `enabled_models` / ENABLED_MODELS.
_DEFAULT_ENABLED_MODELS = _FAIL_OPEN_MODELS | {
    "gpt-5.6-luna-chatgpt",
    "gpt-5.6-sol-chatgpt",
}

# Known frontend model names in preference order. Used only to choose a concrete
# fallback when the requested model is disabled; an allow-list may legitimately
# name models outside this set.
_KNOWN_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-flash-gmi",
    "deepseek-v4-flash-fireworks",
    "deepseek-v4.1-flash-fireworks",
    "claude-4.5-sonnet",
    "claude-4.5-haiku",
    "gpt-5-codex",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-5.4-nano",
    "gpt-5.6-luna",
    # Same two models on the signed-in user's ChatGPT plan (see llm_factory's
    # _CHATGPT_SUBSCRIPTION_MODELS). Listed AFTER the API-key entries so
    # enabled_default_model() never picks a model that needs a user credential.
    "gpt-5.6-luna-chatgpt",
    "gpt-5.6-sol-chatgpt",
    "gemini-3-flash",
    "gemini-3-pro",
    "gemini-3.1-flash-lite",
    "qwen3.7-plus",
    "qwen3.8-27b-hetzner",
    "qwen3-8b-runpod",
    "muse-glimmer-30b-runpod",
    "nemotron-lightning-30b-runpod",
    "nemotron-lightning-30b-fireworks",
    "muse-spark-1.2-contributor",
    "muse-spark-1.3-contributor",
]


def _registry_entry_has_key(name: str) -> bool:
    """True if this box holds a credential for config-registered ``name``.

    Deliberately not ``llm_factory.has_provider_key``: model_policy is imported
    by llm_factory, so calling back into it at module scope would be a cycle.
    The entry already carries the env names; this is the same first-found-wins
    read, without the import.
    """
    entry = get_openrouter_model(name)
    if entry is None:
        return False
    return any(os.getenv(var, "").strip() for var in entry["api_key_env"])


def known_models() -> list:
    """Every model name the policy knows how to fall back to.

    The compiled list plus whatever the OpenRouter registry currently holds, so a
    config-registered model can be chosen as a fallback instead of being invisible
    to the walk in :func:`enabled_default_model`. Registry names go LAST: a
    third-party-routed endpoint should never outrank a first-party model when
    picking what a box runs by default.
    """
    extra = [name for name in openrouter_models() if name not in _KNOWN_MODELS]
    return [*_KNOWN_MODELS, *extra]


def _csv_names(raw: str) -> list:
    """Split a comma-separated env value into a clean list of model names."""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _env_bool(name: str, default: bool) -> bool:
    """Read a boolean operator gate from the environment.

    Unset or blank falls back to ``default``. Anything in the truthy set is True;
    everything else (including an explicit ``false``/``0``/``no``) is False.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def model_switching_allowed() -> bool:
    """True if the instance user may pick a model other than the default."""
    return _env_bool("MODEL_SWITCHING_ALLOWED", _MODEL_SWITCHING_ALLOWED_DEFAULT)


#: Roles a pushed policy may name, and the only ones :func:`role_chain` answers
#: for. An unknown role is dropped rather than stored: nothing resolves it, so
#: keeping it would make :func:`policy_report` describe routing the box does not
#: actually do — which is worse than no answer when someone is reading it mid
#: incident.
_KNOWN_ROLES = ("chat", "vision")


def _clean_role_chains(value) -> Optional[dict]:
    """Validate a ``roles`` mapping, or None when nothing usable survives.

    Each role is validated INDEPENDENTLY, for the same reason the top-level keys
    are: a mistyped ``vision`` chain must not take ``chat`` down with it.
    """
    if not isinstance(value, dict):
        logger.warning("Remote model policy: ignoring malformed roles %r", value)
        return None

    clean: dict = {}
    for role, chain in value.items():
        if role not in _KNOWN_ROLES:
            logger.warning("Remote model policy: ignoring unknown role %r", role)
            continue
        if not isinstance(chain, list) or not all(isinstance(m, str) for m in chain):
            logger.warning(
                "Remote model policy: ignoring malformed %s chain %r", role, chain
            )
            continue
        names = [m.strip() for m in chain if m.strip()]
        if names:
            clean[role] = names
    return clean or None


def _clean_policy_document(raw: dict) -> dict:
    """Validate one policy document — the fleet one, or a per-instance override.

    Factored out precisely so the two cannot drift: an override that skipped a
    check would be a validation hole reachable by exactly the payload that is
    hardest to test from the mothership side.
    """
    clean: dict = {}

    default_model = raw.get("default_model")
    if isinstance(default_model, str) and default_model.strip():
        clean["default_model"] = default_model.strip()
    elif default_model is not None:
        logger.warning("Remote model policy: ignoring non-string default_model %r", default_model)

    for key in ("disabled_models", "enabled_models"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            names = [v.strip() for v in value if v.strip()]
            if names:
                clean[key] = names
        else:
            logger.warning("Remote model policy: ignoring malformed %s %r", key, value)

    roles = raw.get("roles")
    if roles is not None:
        chains = _clean_role_chains(roles)
        if chains:
            clean["roles"] = chains

    return clean


def _apply_instance_override(fleet: dict, instance: dict) -> dict:
    """Merge a per-instance document over the fleet one.

    Narrower scope wins — that is the whole point of the override, and it is why
    one customer's constraint no longer forces a fleet-wide decision.

    ``disabled_models`` is the deliberate exception: it UNIONS, exactly as it
    does across env/instance.json/mothership. A ban that a narrower scope can
    lift is not a ban, and the safe direction is the one where a stale list can
    only ever *widen* a prohibition (see the module docstring, step 1).
    """
    merged = dict(fleet)
    for key, value in instance.items():
        if key == "disabled_models":
            merged[key] = sorted({*fleet.get(key, []), *value})
        else:
            merged[key] = value
    return merged


def remote_policy() -> dict:
    """Model policy the mothership pushed to this box. ``{}`` when none.

    Every key is validated independently and a malformed one is DROPPED rather
    than poisoning the payload: this channel reaches every box in a single lease
    interval, so one bad value must not be able to take the fleet down. Patched
    wholesale in tests.

    The returned document is already flattened: a ``instance_overrides`` block
    has been merged over the fleet-level keys, so every caller sees one answer
    and none of them has to re-derive the precedence. :func:`policy_report` is
    where the provenance is recovered for humans.
    """
    try:
        from app.services import model_policy_store

        raw = model_policy_store.load()
    except Exception as e:  # noqa: BLE001 — never let telemetry config break chat
        logger.warning("Ignoring unreadable remote model policy: %s", e)
        return {}

    clean = _clean_policy_document(raw)

    override = raw.get("instance_overrides")
    if override is None:
        return clean
    if not isinstance(override, dict):
        logger.warning(
            "Remote model policy: ignoring malformed instance_overrides %r", override
        )
        return clean
    return _apply_instance_override(clean, _clean_policy_document(override))


def configured_default_model() -> Optional[str]:
    """The model this box was TOLD to run, or None if nobody said.

    Precedence, most specific first: **mothership > instance.json > env**. The
    compiled constant is deliberately NOT part of this — "nobody configured a
    default" and "the default happens to equal the compiled one" are different
    facts, and only the first should fall through to the normal policy walk.

    The mothership outranks a box's own .env because it is the operator of record
    on a fleet box: a stale hand-edit made during the last incident must not make
    the next remote fix silently no-op on exactly the boxes someone touched.

    ``roles.chat`` and ``default_model`` express the same intent, so one has to
    win: **an explicit ``roles.chat`` does**, and ``default_model`` stays as the
    back-compat spelling so a 0.7.6-era policy document keeps working unchanged.
    The chain wins because it is strictly more informative — it says what to do
    when the first choice is unavailable, which is the entire reason it exists —
    and because a resolved chain has already been checked against this box's
    keys, so it can only ever name something runnable. A chain that resolves to
    nothing falls through to ``default_model`` rather than shadowing it.
    """
    chain = resolve_role("chat")
    if chain:
        return chain

    remote = remote_policy().get("default_model")
    if remote:
        return remote

    config = _read_instance_config() or {}
    instance_default = config.get("default_model")
    if isinstance(instance_default, str) and instance_default.strip():
        return instance_default.strip()

    env_default = (os.getenv("DEFAULT_LLM_MODEL") or "").strip()
    return env_default or None


def _usable_configured_default() -> Optional[str]:
    """The configured default, but only if this box can actually run it.

    The invariant that outranks the operator's intent: the box always resolves to
    a model it can build. A default that is unknown, keyless, or currently
    answering 404 degrades to the normal walk instead of locking chat out — a bad
    remote value must never be more damaging than the outage it was sent to fix.
    """
    name = configured_default_model()
    if not name:
        return None
    if name not in known_models():
        logger.warning(
            "Configured default model %r is not a model this build knows; "
            "ignoring it and resolving normally.", name,
        )
        return None
    if not has_provider_key(name):
        logger.warning(
            "Configured default model %r has no provider key on this box; "
            "ignoring it so turns do not 401. Set the key or change the default.",
            name,
        )
        return None
    if model_health.is_gone(name):
        logger.warning(
            "Configured default model %r is reporting itself retired; routing "
            "around it until the record expires.", name,
        )
        return None
    return name


def default_text_model() -> str:
    """The default model THIS box can actually build.

    ``DEFAULT_LLM_MODEL`` is an intent, not a guarantee: Muse needs a META key,
    which the mothership distributes per box, so a box that has not received one
    yet would otherwise "default" to a model ``get_llm`` constructs with a dud
    key and 401s on every turn. Degrade to the DeepSeek fallback instead — a
    box behind on the rollout chats normally, just without vision.

    Policy (disable lists, allow-lists, the switching lock) is NOT consulted
    here; this answers only "is it buildable". ``enabled_default_model`` layers
    the policy on top.
    """
    configured = _usable_configured_default()
    if configured:
        return configured

    if has_provider_key(DEFAULT_LLM_MODEL):
        return DEFAULT_LLM_MODEL
    return fallback_text_model()


def vision_allowed() -> bool:
    """True if image/video attachments may be sent to a vision model."""
    return _env_bool("VISION_MODEL_ALLOWED", _VISION_ALLOWED_DEFAULT)


def _read_instance_config() -> Optional[dict]:
    """Load instance.json, or None if absent/unreadable. Patched in tests."""
    try:
        with open(_INSTANCE_CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read %s for model policy: %s", _INSTANCE_CONFIG_PATH, e)
        return None


def _instance_list(key: str) -> Optional[list]:
    """Return instance.json[key] as a clean list, or None if absent/empty."""
    config = _read_instance_config()
    if not config:
        return None
    value = config.get(key)
    if isinstance(value, list):
        names = [str(m).strip() for m in value if str(m).strip()]
        return names or None
    return None


def _allowlist() -> Optional[set]:
    """Effective allow-list as a set. Never None — an unconfigured box has one too.

    Intersect each configured source so neither can broaden what the other
    restricts; a source that configures nothing does not constrain. When NO
    source configures anything, the compiled two-model default applies (step 4
    in the module docstring) rather than "everything is enabled".
    """
    env_allow = _csv_names(os.environ.get("ENABLED_MODELS", "")) or None
    remote_allow = remote_policy().get("enabled_models") or None
    allow: Optional[set] = None
    for source in (_instance_list("enabled_models"), env_allow, remote_allow):
        if source is None:
            continue
        source_set = set(source)
        allow = source_set if allow is None else (allow & source_set)
    if allow is None:
        # No allow-list configured anywhere. A box told to run a specific model
        # gets that model, not the compiled two — same reasoning as the fail-open
        # narrowing above.
        configured = _usable_configured_default()
        if configured:
            # ...and the REST of its chat chain with it. An operator who wrote an
            # ordered chain has already said those models are acceptable here;
            # leaving them out of the implied allow-list made rung 2 unable to use
            # the very entries the chain exists to name, so the second choice was
            # "Disabled by administrator" by an administrator who had just chosen
            # it. Explicit allow-lists still intersect normally above — this
            # branch is only the "nobody configured one" case.
            return {configured, *role_chain("chat"), *_CHATGPT_SUBSCRIPTION_MODELS}
        return set(_DEFAULT_ENABLED_MODELS)
    return allow


def _disabled_set() -> set:
    """Models explicitly disabled. Union across sources — any source can disable."""
    disabled: set = set()
    env_disabled = _csv_names(os.environ.get("DISABLED_MODELS", ""))
    instance_disabled = _instance_list("disabled_models") or []
    disabled.update(env_disabled)
    disabled.update(instance_disabled)
    # The mothership can disable too — same union rule as every other source, so
    # a remote ban cannot be broadened away by a stale local list.
    disabled.update(remote_policy().get("disabled_models") or [])
    return disabled


def _fail_open_models() -> frozenset:
    """Models that survive any allow-list, so a box always keeps something runnable.

    Normally the compiled pair. Once the operator names a default this box can
    actually build, that model IS the guarantee and the compiled pair loses the
    privilege — otherwise a retired compiled default stays selectable forever.
    """
    configured = _usable_configured_default()
    if configured:
        return frozenset({configured})
    return _FAIL_OPEN_MODELS


def is_model_enabled(model_name: str) -> bool:
    """True if ``model_name`` is permitted by the operator/mothership policy.

    See the module docstring for the full resolution order.
    """
    # 0. The box's CONFIGURED default outranks even an explicit disable. An
    #    operator who names a default and then disables it has contradicted
    #    himself, and the alternative reading leaves the box with no model at all.
    #    Scoped to an explicitly configured default: disabling the COMPILED
    #    default keeps its old meaning.
    if model_name and model_name == configured_default_model():
        return True
    # 1. Explicit disable wins over everything — even the fail-open defaults.
    if model_name in _disabled_set():
        return False
    # 2. Manual model-switching lock. When switching is off the instance is pinned
    #    to the default text model; the vision model stays reachable only when
    #    vision is also enabled, so the image auto-switch path keeps working. This
    #    sits above the allow-list/fail-open logic — it is the coarse operator
    #    gate — but still below an explicit disable in step 1.
    if not model_switching_allowed():
        # The RESOLVED default, not DEFAULT_LLM_MODEL: on a box with no META key
        # the pin has to land on the model that box can build, or the lock takes
        # chat down entirely instead of merely restricting it.
        if model_name == default_text_model():
            return True
        if model_name == vision_model() and vision_allowed():
            return True
        return False
    # 3. The fail-open defaults are globally enabled (survive any allow-list) —
    #    but a box with a usable CONFIGURED default already has its guaranteed
    #    runnable model, so the compiled pair stops being fail-open. That is what
    #    lets the substitution reach a user whose 365-day llmModel cookie still
    #    names the retired model: while Muse stayed fail-open it remained
    #    "enabled" and effective_model handed it straight back.
    if model_name in _fail_open_models():
        return True
    # 3a. So is the box's resolved vision model, whenever vision is switched on.
    #     Without this, a DeepSeek-only box could never reach the vision model:
    #     the compiled default allow-list (step 4) names only the two blessed
    #     text models, so the operator would have to hand-edit ENABLED_MODELS on
    #     every box just to make image uploads work. Muse-keyed boxes are
    #     unaffected — their vision model is already a fail-open default above.
    #     Still yields to an explicit disable (step 1) and to VISION_MODEL_ALLOWED.
    if model_name and model_name == vision_model() and vision_allowed():
        return True
    # 2b. A registered OpenRouter entry is enabled by its registration — the
    #     registry file is operator-owned, so the act of adding a block IS the
    #     operator saying "offer this". See the module docstring.
    #
    #     ...but only on a box that holds an OPENROUTER_API_KEY. The registry
    #     ships a compiled-in example entry, and without this clause EVERY fleet
    #     box would grow a dropdown option it cannot run, breaking the invariant
    #     that an unconfigured box offers exactly the two blessed models. A key
    #     is what turns the compiled-in default from an example into an offer.
    #     Since 0.7.7 an entry carries its own key env, so the check asks the
    #     entry rather than assuming OPENROUTER_API_KEY — an entry pointed at
    #     another gateway is enabled by ITS key, and a box holding neither still
    #     offers exactly the two blessed models.
    if is_openrouter_model(model_name) and _registry_entry_has_key(model_name):
        return True
    # 4. The allow-list — the box's own, or the compiled two-model default.
    return model_name in _allowlist()


def enabled_default_model() -> str:
    """A concrete enabled model to fall back to. Never raises, never empty.

    Prefers the box's resolved default (see :func:`default_text_model` — the
    project default only when this box can build it); otherwise the first enabled
    known model; if the policy somehow disables every model we know how to build
    (misconfiguration), returns the fallback text model anyway so the instance is
    never locked out of chat.
    """
    # A configured default wins outright, fail-open: the policy lists must not be
    # able to disable the box's own default out from under it and drop it back on
    # the compiled fallback. That is exactly the shape of the 2026-08-31 outage —
    # banning DeepSeek fleet-wide would have put every box ON DeepSeek, because
    # the lockout path returned the compiled constant and ignored the disable list.
    configured = _usable_configured_default()
    if configured:
        return configured

    preferred = default_text_model()
    if is_model_enabled(preferred) and not model_health.is_gone(preferred):
        return preferred
    for name in known_models():
        # A model that just told us it no longer exists is not a fallback. Without
        # this, every turn re-discovers the same 404 and pays for it before
        # answering, for as long as the retirement lasts.
        if model_health.is_gone(name):
            continue
        # Never resolve the box default onto a model paid for by an individual
        # user's ChatGPT plan: a user who has connected nothing could not chat at
        # all. _KNOWN_MODELS lists them last, which used to be enough — it stopped
        # being enough once the compiled default set turned the API-key models
        # off, leaving a subscription model as the first survivor of this walk.
        if name in _CHATGPT_SUBSCRIPTION_MODELS:
            continue
        # Nor onto a model that exists only as a vision switch target. Step 2a
        # can enable one on a box whose allow-list names nothing else, and this
        # walk would then hand it every text turn as well.
        if name in _VISION_ONLY_MODELS:
            continue
        # Nor onto a config-registered OpenRouter endpoint. Step 2b enables those
        # wherever a key exists, so without this the lockout fallback would quietly
        # move a box onto a third-party-routed endpoint someone added to try — a
        # thing a user PICKS, never what a box falls back to running.
        if is_openrouter_model(name):
            continue
        if is_model_enabled(name):
            return name
    lockout_fallback = fallback_text_model()
    logger.warning(
        "Model policy disables all known models; falling back to %s. "
        "Check enabled_models/disabled_models in instance.json and "
        "ENABLED_MODELS/DISABLED_MODELS. Set DEFAULT_LLM_MODEL (or "
        "FALLBACK_TEXT_MODEL) to choose what a locked-out box runs.",
        lockout_fallback,
    )
    return lockout_fallback


def fallback_model(
    primary: str,
    *,
    needs_vision: bool = False,
    exclude=(),
) -> Optional[str]:
    """A different model to finish this step on when ``primary`` stops answering.

    Rung 2 of the resilience ladder (docs/dev/error_telemetry.md §3), which until
    0.7.5 was never built: when the Muse Spark endpoint accepted requests and
    streamed nothing on 2026-08-26, rung 1 retried that same dead endpoint five
    times and there was nowhere else to go.

    Resolved from policy, never from a literal model id — the rule
    ``test_no_agent_hardcodes_a_fallback_model`` exists to enforce, after every
    ``rails_*`` agent spent 0.7.0 naming a DeepSeek id inline as its fallback and
    silently ignoring the fleet default. (That sweep is line-based over source
    text, so even writing the offending idiom in a comment here trips it — as it
    should: this is the one function most tempted to reintroduce it.)

    Candidates must be BOTH enabled by policy and buildable on this box's keys.
    A fallback the box cannot build is a second failure, and one policy disables
    is one ``get_llm`` would immediately substitute straight back — which on a
    single-model box would be the model that just stalled.

    ``needs_vision`` keeps the image routing rule intact across the fallback: a
    turn carrying a screenshot must not be finished by a model that cannot see
    it, because answering the question without the image is worse than failing.

    Returns ``None`` when there is genuinely nowhere to go (a box pinned to one
    model, or an image turn with a single vision-capable model). The caller must
    treat that as "no rung 2" and let the original error surface.
    """
    tried = {primary, *exclude}

    if needs_vision:
        if not vision_allowed():
            return None
        # The vision ROLE CHAIN, not the compiled tuple: an operator who pushed
        # roles.vision has already said what an image turn should degrade to, and
        # rung 2 is exactly the moment that answer is needed. role_chain falls
        # back to the compiled tuple when nothing is pushed, so an unconfigured
        # box behaves as it did in 0.7.6.
        for name in role_chain("vision"):
            if name in tried:
                continue
            if has_provider_key(name) and is_model_enabled(name):
                return name
        return None

    # An explicit chat chain outranks everything below, for the same reason it
    # outranks default_model in configured_default_model: it is the operator
    # saying what to do when the first choice is unavailable, which is precisely
    # the question being asked here. Entries already tried are skipped so a turn
    # cannot bounce between two endpoints it has already burned.
    for name in role_chain("chat"):
        if name in tried:
            continue
        if model_health.is_gone(name):
            continue
        if is_model_enabled(name) and has_provider_key(name):
            return name

    # The box's own default first: it is what the operator chose to run, and on
    # the Muse box in the incident it is also the model that stalled — hence the
    # tried check rather than returning it outright.
    preferred = enabled_default_model()
    if (preferred not in tried and has_provider_key(preferred)
            and not model_health.is_gone(preferred)):
        return preferred

    for name in known_models():
        if name in tried:
            continue
        # The same three exclusions enabled_default_model() walks past, for the
        # same reasons: never spend an individual user's ChatGPT plan, never hand
        # a text turn to a vision-only switch target, and never quietly move a
        # box onto a third-party-routed endpoint someone registered to try out.
        if name in _CHATGPT_SUBSCRIPTION_MODELS:
            continue
        if name in _VISION_ONLY_MODELS:
            continue
        if is_openrouter_model(name):
            continue
        # Rung 2 exists to finish the turn somewhere else; a model that just told
        # us it is retired is the one place guaranteed not to work.
        if model_health.is_gone(name):
            continue
        if is_model_enabled(name) and has_provider_key(name):
            return name
    return None


def effective_model(model_name: str) -> str:
    """The model a request for ``model_name`` will actually run on.

    Single source of truth for the substitution :func:`get_llm` performs, so the
    websocket layer can warn the user that their pick was swapped without
    re-deriving the rule here and drifting from it later. Substitution used to be
    silent: the dropdown kept showing the model the user chose while every turn
    ran on the default, and the only trace was a WARNING in the container log.
    """
    # A model that reported itself retired is never "effective", whatever the policy
    # says. An allow-list is the operator stating a model MAY be used; it is not a
    # claim that the model still exists. Without this a box whose instance.json names
    # the retired model kept handing it back — and because the llmModel cookie lives
    # 365 days, this function is the only thing that reaches an already-pinned user.
    if model_health.is_gone(model_name):
        return enabled_default_model()
    if is_model_enabled(model_name):
        return model_name
    return enabled_default_model()


# =============================================================================
# Provenance — what the box resolved, and which channel said so
# =============================================================================
#
# Remote control is only half a feature without this. During the 2026-08-31
# incident the question that cost the most time was not "what should this box
# run" but "what does this box think it is running, and who told it that" — and
# the only way to answer it was to SSH in and read three files. A policy that can
# be changed from the mothership has to be READABLE from outside the box too, or
# every push is a guess followed by a wait.
#
# Source names are a closed set, most-specific first:
#
#   mothership-instance  this box's own override in the pushed document
#   mothership-fleet     the fleet-wide pushed document
#   instance.json        the box's provisioned config file
#   env                  DEFAULT_LLM_MODEL and friends, baked into the container
#   compiled             nobody configured anything; the code's own default won

def _raw_remote_document() -> dict:
    """The pushed document BEFORE the instance override is merged in.

    :func:`remote_policy` deliberately flattens the two so no caller has to
    re-derive precedence; provenance is the one question that needs them apart.
    """
    try:
        from app.services import model_policy_store

        raw = model_policy_store.load()
    except Exception:  # noqa: BLE001 — provenance never breaks chat
        return {}
    return raw if isinstance(raw, dict) else {}


def _roles_source() -> str:
    """Which channel supplied the role chains this box is using."""
    raw = _raw_remote_document()
    override = raw.get("instance_overrides")
    if isinstance(override, dict) and _clean_role_chains(override.get("roles") or {}):
        return "mothership-instance"
    if _clean_role_chains(raw.get("roles") or {}):
        return "mothership-fleet"
    config = _read_instance_config() or {}
    if _clean_role_chains(config.get("roles") or {}):
        return "instance.json"
    return "compiled"


def _configured_default_source() -> str:
    """Which channel named the default, or "" if nobody did."""
    if resolve_role("chat"):
        return _roles_source()

    if remote_policy().get("default_model"):
        override = _raw_remote_document().get("instance_overrides")
        if isinstance(override, dict) and isinstance(override.get("default_model"), str):
            return "mothership-instance"
        return "mothership-fleet"

    config = _read_instance_config() or {}
    instance_default = config.get("default_model")
    if isinstance(instance_default, str) and instance_default.strip():
        return "instance.json"

    if (os.getenv("DEFAULT_LLM_MODEL") or "").strip():
        return "env"
    return ""


def policy_report() -> dict:
    """The resolved routing plus its provenance, for ``/api/available-models``.

    Reports what the box ACTUALLY resolved, not what it was told: a configured
    default the box cannot build is reported as ``compiled``, because that is
    what is really answering turns. Reporting the intent instead would make this
    endpoint agree with the mothership and disagree with reality, which is the
    one failure mode a debugging aid must not have.

    Safe to expose to any signed-in user: model names are already published by
    the dropdown, and no key name, key value or file path appears here.
    """
    configured_source = _configured_default_source()
    default_source = configured_source if _usable_configured_default() else "compiled"

    resolved_vision = vision_model()
    vision_source = _roles_source() if policy_roles().get("vision") else "compiled"

    return {
        "default_model": {
            "value": enabled_default_model(),
            "source": default_source or "compiled",
        },
        "vision_model": {
            "value": resolved_vision,
            "source": vision_source,
        },
        "roles": policy_roles(),
        "roles_source": _roles_source(),
        # Which config-registered models this box currently sees, so a remote
        # push can be confirmed from outside the box (0.7.7). Names only — the
        # entry itself carries an api_base and a key env, and this endpoint is
        # readable by every signed-in user. Sorted so a diff between two boxes
        # is a diff, not an ordering artifact.
        "registered_models": sorted(openrouter_models()),
    }
