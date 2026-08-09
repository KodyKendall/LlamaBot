"""The /api/cookbook proxy behind the /cookbook slash menu.

llamapress.ai/cookbook.json sends no CORS headers, so the browser cannot read it
directly — the menu depends on this proxy. What matters here: the published shape
is normalized (and junk entries dropped), repeat opens hit the cache instead of
llamapress.ai, and a failed fetch serves the last good list rather than emptying
the menu.
"""

import asyncio

import pytest

from app.routers import api


@pytest.fixture(autouse=True)
def clear_cookbook_cache():
    api._cookbook_cache["guides"] = None
    api._cookbook_cache["fetched_at"] = 0.0
    yield
    api._cookbook_cache["guides"] = None
    api._cookbook_cache["fetched_at"] = 0.0


def test_normalize_shapes_the_published_index():
    guides = api._normalize_cookbook_guides({
        "guides": [{
            "slug": "pdf-download-export",
            "title": "PDF Download Export",
            "category": "Reports",
            "summary": "Render any page as a PDF.",
            "tags": ["pdf", "export"],
            "extra_field_we_dont_use": True,
        }]
    })

    assert guides == [{
        "slug": "pdf-download-export",
        "title": "PDF Download Export",
        "category": "Reports",
        "summary": "Render any page as a PDF.",
        "tags": ["pdf", "export"],
        "url": "https://llamapress.ai/cookbook/pdf-download-export",
    }]


def test_normalize_drops_unusable_entries():
    guides = api._normalize_cookbook_guides({
        "guides": [
            {"title": "No slug — nothing to link to"},
            {"slug": ""},
            "not-a-dict",
            {"slug": "ok"},
        ]
    })

    assert [g["slug"] for g in guides] == ["ok"]
    assert guides[0]["title"] == "ok"  # falls back to the slug


def test_normalize_accepts_a_bare_list_and_junk():
    assert [g["slug"] for g in api._normalize_cookbook_guides([{"slug": "a"}])] == ["a"]
    assert api._normalize_cookbook_guides(None) == []
    assert api._normalize_cookbook_guides("nope") == []


def test_second_call_is_served_from_cache(monkeypatch):
    calls = []

    async def fake_fetch():
        calls.append(1)
        return [{"slug": "a", "title": "A", "category": "", "summary": "", "tags": [], "url": "u"}]

    monkeypatch.setattr(api, "_fetch_cookbook_index", fake_fetch)

    first = asyncio.run(api.api_get_cookbook(username="tester"))
    second = asyncio.run(api.api_get_cookbook(username="tester"))

    assert len(calls) == 1, "opening the menu twice must not re-hit llamapress.ai"
    assert first == second
    assert first["stale"] is False
    assert [g["slug"] for g in first["guides"]] == ["a"]


def test_a_failed_fetch_serves_the_last_good_list(monkeypatch):
    async def ok_fetch():
        return [{"slug": "a", "title": "A", "category": "", "summary": "", "tags": [], "url": "u"}]

    monkeypatch.setattr(api, "_fetch_cookbook_index", ok_fetch)
    asyncio.run(api.api_get_cookbook(username="tester"))

    # Expire the cache, then make the upstream fail.
    api._cookbook_cache["fetched_at"] = 0.0

    async def boom():
        raise RuntimeError("llamapress.ai unreachable")

    monkeypatch.setattr(api, "_fetch_cookbook_index", boom)
    result = asyncio.run(api.api_get_cookbook(username="tester"))

    assert [g["slug"] for g in result["guides"]] == ["a"]
    assert result["stale"] is True


def test_a_failure_with_no_cache_returns_an_empty_list_not_an_error(monkeypatch):
    async def boom():
        raise RuntimeError("llamapress.ai unreachable")

    monkeypatch.setattr(api, "_fetch_cookbook_index", boom)
    result = asyncio.run(api.api_get_cookbook(username="tester"))

    assert result["guides"] == []
    assert result["stale"] is True
