"""Layered LangGraph agent registry: platform base + client overlay.

The agent-graph registry (`langgraph.json`) is split by *ownership* into two tiers so
that platform updates and client customization never collide:

  * ``langgraph.json``       — the PLATFORM base. Baked into the LlamaBot image and (in
                               Leonardo) tracked from upstream wholesale. Safe to clobber
                               on a platform sync because it holds NO client keys.
  * ``langgraph.local.json`` — the CLIENT overlay. Downstream-owned, host-mounted, never
                               synced/clobbered. Holds client-registered graphs and is the
                               write target for the rails AI-builder's edit tool, so
                               agent-registered graphs survive a container recreate.

Optionally, a ``langgraph.d/`` directory of ``*.json`` drop-ins (sorted by filename) is
merged between the base and the local overlay (see order below). All overlays contribute
only a ``graphs`` map; ``dependencies`` / ``env`` and other top-level keys stay
platform-owned (read from the base only).

Merge order (later wins on a key collision):

    base.graphs  <  langgraph.d/*.json (sorted)  <  langgraph.local.json

so ``langgraph.local.json`` is the highest-precedence, human/agent-editable layer, and a
client can register new agents — or deliberately shadow a platform one — without ever
touching the base.

Every read path in the app funnels through here: the runtime graph resolver
(``request_handler``), the ``/agents`` endpoint, the custom-agent-mode validator, and the
AI-builder's ``read_langgraph_json`` tool. Fail-open everywhere: a missing or malformed
overlay is logged and skipped so a broken client file can never take down the registry.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BASE_NAME = "langgraph.json"
OVERLAY_NAME = "langgraph.local.json"
DROPIN_DIR = "langgraph.d"

# Env overrides (handy in containers / CI):
#   LANGGRAPH_CONFIG       — explicit path to the base file
#   LANGGRAPH_LOCAL_CONFIG — explicit path to the client overlay file
ENV_BASE = "LANGGRAPH_CONFIG"
ENV_OVERLAY = "LANGGRAPH_LOCAL_CONFIG"


def resolve_base_path(start: Optional[Path] = None) -> Optional[Path]:
    """Locate the platform base ``langgraph.json``.

    Priority: ``$LANGGRAPH_CONFIG`` → nearest ``langgraph.json`` walking up from the
    current working directory → nearest one walking up from this module. Returns None if
    none is found (callers treat that as "empty registry").
    """
    explicit = os.getenv(ENV_BASE)
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p
        logger.warning("%s='%s' is not a file — falling back to path search", ENV_BASE, p)

    starts = []
    if start is not None:
        starts.append(Path(start))
    starts.append(Path.cwd())
    starts.append(Path(__file__).resolve().parent)

    for s in starts:
        s = s.resolve()
        for parent in [s, *s.parents]:
            candidate = parent / BASE_NAME
            if candidate.is_file():
                return candidate
    return None


def local_overlay_path(base_path: Optional[Path] = None) -> Path:
    """Path to the client overlay file (the AI-builder's write target).

    Honors ``$LANGGRAPH_LOCAL_CONFIG``; otherwise it is a sibling of the base file.
    Returned even when the file does not yet exist so callers can create it on first write.
    """
    explicit = os.getenv(ENV_OVERLAY)
    if explicit:
        return Path(explicit).expanduser()
    base = Path(base_path) if base_path is not None else resolve_base_path()
    base_dir = base.parent if base is not None else Path.cwd()
    return base_dir / OVERLAY_NAME


def overlay_paths_for(base_path: Path) -> list[Path]:
    """Ordered list of existing overlay files to merge over the base.

    Order = ``langgraph.d/*.json`` (sorted by name) then ``langgraph.local.json``, i.e.
    the local overlay is highest precedence. Only existing files are returned.
    """
    base_dir = Path(base_path).parent
    paths: list[Path] = []

    dropin = base_dir / DROPIN_DIR
    if dropin.is_dir():
        paths.extend(sorted(p for p in dropin.glob("*.json") if p.is_file()))

    overlay = local_overlay_path(base_path)
    if overlay.is_file():
        paths.append(overlay)

    return paths


def _read_graphs(path: Path) -> dict:
    """Read the ``graphs`` map from a JSON file, fail-open to ``{}`` on any problem."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError) as e:
        logger.warning("langgraph registry: could not read %s (%s)", path, e)
        return {}
    except json.JSONDecodeError as e:
        logger.warning("langgraph registry: invalid JSON in %s (%s) — ignoring", path, e)
        return {}

    if not isinstance(data, dict):
        logger.warning("langgraph registry: %s is not a JSON object — ignoring", path)
        return {}
    graphs = data.get("graphs", {})
    if not isinstance(graphs, dict):
        logger.warning("langgraph registry: 'graphs' in %s is not an object — ignoring", path)
        return {}
    return graphs


def load_registry(base_path: Optional[Path] = None) -> dict:
    """Return the full merged config dict.

    Top-level keys (``dependencies``, ``env``, ...) come from the base only; ``graphs`` is
    the base map with every overlay merged over it (overlay wins on collision). Returns
    ``{"graphs": {}}`` if no base file can be located.
    """
    base = Path(base_path) if base_path is not None else resolve_base_path()
    if base is None or not base.is_file():
        logger.warning("langgraph registry: no base %s found — empty registry", BASE_NAME)
        return {"graphs": {}}

    try:
        with base.open("r", encoding="utf-8") as f:
            registry = json.load(f)
        if not isinstance(registry, dict):
            raise ValueError("base langgraph.json is not a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning("langgraph registry: base %s unreadable (%s) — empty registry", base, e)
        return {"graphs": {}}

    graphs = dict(registry.get("graphs", {}) if isinstance(registry.get("graphs"), dict) else {})
    for overlay in overlay_paths_for(base):
        graphs.update(_read_graphs(overlay))

    registry["graphs"] = graphs
    return registry


def load_graphs(base_path: Optional[Path] = None) -> dict:
    """Convenience: just the merged ``graphs`` map (base ∪ overlays, overlay wins)."""
    return load_registry(base_path).get("graphs", {})
