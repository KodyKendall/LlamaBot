"""The /cookbook menu must show the owner's OWN recipes, not just the fleet's (0.7.5).

Every LlamaPress user now has a personal cookbook — user-namespaced recipes published from
their own Leo boxes (mothership, 2026-08-28; asked for by a Business-plan user running four
Leos who wants to reuse UI patterns across them). Until now `/api/cookbook` proxied only the
fleet index, so a recipe the user published from one box never appeared on any of their
others, and the agents did not know personal cookbooks existed at all.

Personal recipes are an ENHANCEMENT: a local dev box with no mothership config, or a
mothership that is down, must still get a working fleet menu.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routers.api import _merge_personal_cookbook, _normalize_personal_recipes


FLEET = [
    {"slug": "pdf-export", "title": "PDF export", "category": "docs", "url": "https://llamapress.ai/cookbook/pdf-export"},
]

PERSONAL_PAYLOAD = {
    "handle": "kody",
    "recipes": [
        {"slug": "glow-toggle", "title": "Glow toggle", "summary": "A toggle that glows",
         "category": "ui", "visibility": "public", "updated_at": "2026-08-28T00:00:00Z"},
        {"slug": "secret-thing", "title": "Unlisted thing", "category": "ui",
         "visibility": "unlisted", "updated_at": "2026-08-28T00:00:00Z"},
    ],
}


def test_personal_recipes_are_normalized_to_the_menu_shape():
    guides = _normalize_personal_recipes(PERSONAL_PAYLOAD)

    assert [g["slug"] for g in guides] == ["glow-toggle", "secret-thing"]
    first = guides[0]
    assert first["title"] == "Glow toggle"
    # The .json and .md of this URL both exist, so the frontend's existing
    # cookbookJsonUrl mention mechanics keep working with no change.
    assert first["url"] == "https://llamapress.ai/cookbook/u/kody/glow-toggle"
    assert first["personal"] is True


def test_unlisted_recipes_are_included_because_they_are_the_owner_s_own():
    guides = _normalize_personal_recipes(PERSONAL_PAYLOAD)
    assert "secret-thing" in [g["slug"] for g in guides]


def test_personal_recipes_rank_above_fleet_guides():
    merged = _merge_personal_cookbook(FLEET, _normalize_personal_recipes(PERSONAL_PAYLOAD))

    assert merged[0]["personal"] is True
    assert merged[-1]["slug"] == "pdf-export"
    assert len(merged) == 3


def test_a_personal_recipe_shadows_a_fleet_guide_with_the_same_slug():
    """The user's own version of a recipe is the one they meant."""
    personal = _normalize_personal_recipes(
        {"handle": "kody", "recipes": [{"slug": "pdf-export", "title": "My PDF export"}]})

    merged = _merge_personal_cookbook(FLEET, personal)

    assert len(merged) == 1
    assert merged[0]["title"] == "My PDF export"
    assert merged[0]["personal"] is True


def test_fleet_menu_survives_a_missing_personal_cookbook():
    assert _merge_personal_cookbook(FLEET, []) == FLEET
    assert _merge_personal_cookbook(FLEET, None) == FLEET


def test_a_malformed_personal_payload_degrades_to_nothing():
    for junk in (None, {}, {"recipes": "not a list"}, {"recipes": [{"no": "slug"}]}, "nope"):
        assert _normalize_personal_recipes(junk) == []


def test_personal_recipes_need_a_handle_to_build_a_url():
    """Without a handle there is no resolvable URL, so the entry is useless — drop it."""
    assert _normalize_personal_recipes({"recipes": [{"slug": "x", "title": "X"}]}) == []


def test_client_returns_none_when_the_box_has_no_mothership_config():
    from app.services.mothership_client import MothershipClient

    client = MothershipClient()
    client.config = {"instance_name": "", "mothership_url": "", "mothership_api_token": ""}

    assert asyncio.run(client.get_personal_cookbook()) is None


def test_client_sends_instance_name_as_a_query_param():
    """The mothership reads params[:instance_name] on this GET, not a JSON body."""
    from app.services.mothership_client import MothershipClient

    client = MothershipClient()
    client.config = {
        "instance_name": "leo-test",
        "mothership_url": "https://llamapress.ai",
        "mothership_api_token": "token",
    }

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json = MagicMock(return_value=PERSONAL_PAYLOAD)

    with patch("httpx.AsyncClient") as ac:
        instance = ac.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=response)
        with patch.object(MothershipClient, "reporting_enabled", True):
            result = asyncio.run(client.get_personal_cookbook())

    assert result == PERSONAL_PAYLOAD
    kwargs = instance.get.await_args.kwargs
    assert kwargs["params"] == {"instance_name": "leo-test"}
    assert kwargs["headers"]["Authorization"] == "Bearer token"


def test_client_returns_none_rather_than_raising_when_the_mothership_errors():
    from app.services.mothership_client import MothershipClient

    client = MothershipClient()
    client.config = {
        "instance_name": "leo-test",
        "mothership_url": "https://llamapress.ai",
        "mothership_api_token": "token",
    }

    with patch("httpx.AsyncClient") as ac:
        instance = ac.return_value.__aenter__.return_value
        instance.get = AsyncMock(side_effect=RuntimeError("mothership down"))
        with patch.object(MothershipClient, "reporting_enabled", True):
            assert asyncio.run(client.get_personal_cookbook()) is None
