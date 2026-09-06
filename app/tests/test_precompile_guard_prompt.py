"""Leo must not run `assets:precompile` on a customer box.

The app runs in development, where Rails compiles on demand. `assets:precompile`
writes `public/assets` plus a manifest, and Rails then serves those frozen copies
instead of the files Leo edits — so every later CSS/JS change silently stops
reaching the browser.

Leo ran it six times on one box (leo-sepe, 2026-09-03) while chasing what turned
out to be the TMPDIR bug. It failed each time, which is the only reason that box
is not frozen today.
"""

from pathlib import Path

PROMPTS = (
    Path(__file__).resolve().parents[1]
    / "agents" / "leonardo" / "rails_agent" / "prompts.py"
).read_text()


def test_the_prompt_tells_leo_not_to_precompile():
    assert "Never Run `assets:precompile`" in PROMPTS


def test_it_says_what_goes_wrong_not_just_dont():
    """A bare prohibition gets reasoned around; the consequence does not."""
    section = PROMPTS[PROMPTS.index("Never Run `assets:precompile`"):][:1200]
    assert "public/assets" in section
    assert "development" in section


def test_it_names_the_thing_to_do_instead():
    section = PROMPTS[PROMPTS.index("Never Run `assets:precompile`"):][:1200]
    assert "tailwindcss:build" in section
