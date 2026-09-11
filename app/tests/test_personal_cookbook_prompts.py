"""All four Cookbook prompt sections must know personal cookbooks exist (0.7.5).

The agents were told about the FLEET cookbook only. A user who publishes a recipe from one
of their Leos — the feature a Business-plan user with four boxes asked for — would find Leo
on the next box had no idea it existed, could not follow a /cookbook/u/<handle>/<slug>.json
mention, and had no way to save a pattern when asked to.

Structural assertions only: that each prompt names the personal namespace and the publish
guide. Never an assertion about what the model then says.
"""
from pathlib import Path

import pytest

PROMPT_FILES = [
    "rails_agent/prompts.py",
    "rails_plan_mode_agent/prompts.py",
    "rails_ticket_mode_agent/prompts.py",
    "rails_beginner_agent/prompts.py",
]

AGENTS = Path(__file__).resolve().parents[1] / "agents" / "leonardo"


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_prompt_mentions_the_personal_cookbook_namespace(relative):
    source = (AGENTS / relative).read_text()

    assert "/cookbook/u/" in source, (
        f"{relative} never mentions the personal cookbook namespace, so Leo cannot follow "
        "an @cookbook: mention pointing at one of the user's own recipes."
    )


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_prompt_points_at_the_publish_guide(relative):
    """'Save this to my cookbook' has to lead somewhere."""
    source = (AGENTS / relative).read_text()

    assert "publish-to-your-personal-cookbook.md" in source, (
        f"{relative} does not tell Leo where the publish flow is documented."
    )


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_the_fleet_cookbook_is_still_described(relative):
    """The personal cookbook is an addition, not a replacement."""
    source = (AGENTS / relative).read_text()

    assert "cookbook.json" in source


@pytest.mark.parametrize("relative", PROMPT_FILES)
def test_prompt_does_not_send_leo_to_the_environment_for_the_token(relative):
    """The publish flow must not name a variable the exec scrub blanks (0.7.7).

    ``MOTHERSHIP_API_TOKEN`` is not in ``_EXEC_ENV_ALLOWLIST`` (rails_agent/tools.py),
    and that list is default-deny, so ``build_exec_env`` blanks it for every
    ``bash_command`` exec. Leo read an empty string, sent ``Authorization: Bearer ``
    and the mothership answered ``{"success":false,"error":"Missing credentials"}``.
    The customer on box leo-zuset was told the app "does not have the required
    cookbook sign-in credentials" and left a thumbs-down (2026-09-01).

    The credentials themselves are fine — they are readable from
    ``/rails/.leonardo/instance.json``, which the scrub does not touch, and the
    published guide now documents that path. Only the prompt's pointer was wrong.

    Same failure mode as the ``HOSTED_DOMAIN`` note already in that allowlist:
    "scrubbing it made the documented command return an empty string."
    """
    source = (AGENTS / relative).read_text()

    assert "MOTHERSHIP_API_TOKEN" not in source, (
        f"{relative} sends Leo to an environment variable the exec scrub blanks. "
        "Point at the published guide instead; it documents where the credentials live."
    )


# ---------------------------------------------------------------------------
# The prompt paragraphs above are DEAD CODE on every real box (0.7.9).
#
# resolve_base_prompt() prefers the mothership-delivered cached body whenever it is
# >= MIN_PROMPT_LEN, and the mothership serves nine prompt files seeded byte-for-byte
# from LlamaBot 0.4.1 on 2026-06-27 and never touched since. Measured on the
# customer's own box leo-loepo (llamabot 0.7.7), table agent_system_prompts:
#
#   agent_mode  | version | len(body) | pos('PERSONAL cookbook') | pos('@cookbook')
#   rails_agent | 0ced1cf | 74599     | 0                        | 0
#   ... all nine rows: 0 and 0.
#
# So the tests above pass, the paragraphs are really in the source, and the fleet has
# never seen a word of them. Richie (vicetheorystudios@gmail.com) published
# `lionhearted-metallic-gold` from leo-loepo and could only use it on his other box by
# opening the /cookbook menu and picking it by hand; asking for it in words did nothing.
#
# The fix is an ADDENDUM appended after resolve_base_prompt() returns — the same
# treatment LEONARDO.md and MEMORY.md get. An addendum survives the override; a prompt
# edit does not. These tests own that, and they are the regression that matters.
# ---------------------------------------------------------------------------

from unittest.mock import patch

from app.agents.leonardo import personal_cookbook_context as pcc
from app.agents.leonardo import project_context


# A cached mothership body long enough to win resolve_base_prompt's MIN_PROMPT_LEN
# check, and containing NONE of the cookbook text — i.e. the real 0.4.1-seeded body.
STALE_OVERRIDE = (
    "You are Leo, a helpful Rails engineer. " * 20
    + "\nTo browse shared patterns: curl https://llamapress.ai/cookbook.json\n"
)

PAYLOAD = {
    "handle": "vicetheorystudios",
    "recipes": [
        {
            "slug": "lionhearted-metallic-gold",
            "title": "Lionhearted Metallic Gold — Brushed-Metal Design System",
            "summary": "Secondary-accent gold on near-black.",
            "updated_at": "2026-09-11T17:31:00Z",
        },
    ],
}


@pytest.fixture(autouse=True)
def _clean_cookbook_cache():
    """Every test starts from a cold cache and leaves one behind."""
    pcc.reset_personal_cookbook_cache()
    yield
    pcc.reset_personal_cookbook_cache()


def _prime(payload):
    pcc.store_personal_recipes(pcc.normalize_personal_recipes(payload))


def _build(base="STATIC BASE PROMPT", mode="rails_agent", cached=STALE_OVERRIDE):
    with patch.object(project_context.system_prompt_cache, "get_cached", return_value=cached):
        return project_context.build_system_prompt_with_project_context(base, agent_mode=mode)


def _build_beginner(base="STATIC BASE PROMPT", mode="rails_beginner_agent", cached=STALE_OVERRIDE):
    with patch.object(project_context.system_prompt_cache, "get_cached", return_value=cached):
        return project_context.build_beginner_system_prompt(base, agent_mode=mode)


class TestTheAddendumSurvivesTheMothershipOverride:
    """The one that matters: the block has to land on a box whose prompt was replaced."""

    def test_the_override_really_does_replace_the_static_prompt(self):
        """Guard the premise. If this ever fails, the whole approach is unnecessary."""
        built = _build(base="STATIC BASE PROMPT")

        assert "STATIC BASE PROMPT" not in built
        assert "PERSONAL cookbook" not in STALE_OVERRIDE

    def test_the_recipes_appear_anyway(self):
        _prime(PAYLOAD)

        built = _build()

        assert "lionhearted-metallic-gold" in built
        assert "vicetheorystudios" in built

    def test_it_carries_a_fetchable_url_not_just_a_name(self):
        """A slug Leo cannot resolve to a URL is no better than no slug."""
        _prime(PAYLOAD)

        built = _build()

        assert (
            "https://llamapress.ai/cookbook/u/vicetheorystudios/lionhearted-metallic-gold.md"
            in built
        )

    def test_it_points_at_the_publish_guide_rather_than_the_environment(self):
        """Same 0.7.7 trap as the prompt files: MOTHERSHIP_API_TOKEN is scrubbed by exec."""
        _prime(PAYLOAD)

        built = _build()

        assert "publish-to-your-personal-cookbook.md" in built
        assert "MOTHERSHIP_API_TOKEN" not in built

    def test_the_beginner_builder_gets_it_too(self):
        _prime(PAYLOAD)

        built = _build_beginner()

        assert "lionhearted-metallic-gold" in built


class TestABoxWhoseOwnerPublishedNothingPaysNothing:
    def test_no_recipes_cached_means_no_block(self):
        built = _build()

        assert "Personal Cookbook" not in built

    def test_an_empty_recipe_list_means_no_block(self):
        _prime({"handle": "someone", "recipes": []})

        built = _build()

        assert "Personal Cookbook" not in built

    def test_a_payload_with_no_handle_means_no_block(self):
        """Without a handle there is no resolvable URL, so the block would be useless."""
        _prime({"recipes": [{"slug": "orphan"}]})

        built = _build()

        assert "orphan" not in built


class TestItCanNeverBreakATurn:
    def test_a_none_payload_yields_no_block_and_no_raise(self):
        _prime(None)

        assert pcc.build_personal_cookbook_context() == ""

    def test_a_cache_read_that_explodes_is_swallowed(self):
        with patch.object(pcc, "get_cached_personal_recipes", side_effect=RuntimeError("boom")):
            assert pcc.build_personal_cookbook_context() == ""

    def test_a_garbage_entry_does_not_take_the_block_down(self):
        _prime({"handle": "h", "recipes": [{"slug": "good", "title": "Good"}, "not-a-dict", None]})

        built = _build()

        assert "good" in built


class TestThePromptPathMakesNoNetworkCall:
    """Prompt building is synchronous and runs on EVERY turn. A 5s httpx call here
    would add latency to every message and could hang a turn outright."""

    def test_building_a_prompt_never_touches_the_mothership_client(self):
        _prime(PAYLOAD)

        class ExplodingClient:
            def __getattr__(self, name):
                raise AssertionError(
                    f"prompt building called MothershipClient.{name}() — the prompt path "
                    "must read the cache only"
                )

        with patch.object(pcc, "_build_client", return_value=ExplodingClient()):
            built = _build()

        assert "lionhearted-metallic-gold" in built

    def test_building_a_prompt_never_calls_httpx(self):
        import httpx

        _prime(PAYLOAD)

        def _boom(*a, **k):
            raise AssertionError("prompt building made an HTTP call")

        with patch.object(httpx, "AsyncClient", _boom), patch.object(httpx, "Client", _boom):
            built = _build()

        assert "lionhearted-metallic-gold" in built


class TestTheBlockIsBudgeted:
    """A user with hundreds of recipes must not silently eat the context window."""

    def test_it_caps_the_number_of_recipes(self):
        _prime({
            "handle": "prolific",
            "recipes": [
                {"slug": f"recipe-{i:03d}", "title": f"Recipe {i}", "updated_at": f"2026-01-{i % 28 + 1:02d}"}
                for i in range(200)
            ],
        })

        block = pcc.build_personal_cookbook_context()

        assert block.count("- `recipe-") <= pcc.MAX_RECIPES

    def test_it_caps_the_total_size(self):
        _prime({
            "handle": "verbose",
            "recipes": [
                {"slug": f"r-{i}", "title": "T" * 500, "summary": "S" * 2000, "updated_at": "2026-01-01"}
                for i in range(60)
            ],
        })

        block = pcc.build_personal_cookbook_context()

        assert len(block) <= pcc.MAX_BLOCK_CHARS

    def test_newest_first_so_a_truncated_list_keeps_what_they_just_published(self):
        _prime({
            "handle": "h",
            "recipes": [
                {"slug": "ancient", "title": "Ancient", "updated_at": "2020-01-01T00:00:00Z"},
                {"slug": "newest", "title": "Newest", "updated_at": "2026-09-11T17:31:00Z"},
                {"slug": "middle", "title": "Middle", "updated_at": "2024-05-05T00:00:00Z"},
            ],
        })

        block = pcc.build_personal_cookbook_context()

        assert block.index("newest") < block.index("middle") < block.index("ancient")


class TestTheRouterAndThePromptShareOneCache:
    """api.py's slash-menu fetch and the prompt builder must not drift apart."""

    def test_the_router_imports_the_shared_cache(self):
        from app.routers import api

        assert api._normalize_personal_recipes is pcc.normalize_personal_recipes

    @pytest.mark.asyncio
    async def test_a_router_refresh_is_visible_to_the_prompt_builder(self):
        class Client:
            async def get_personal_cookbook(self):
                return PAYLOAD

        await pcc.refresh_personal_cookbook(Client())

        assert "lionhearted-metallic-gold" in pcc.build_personal_cookbook_context()

    @pytest.mark.asyncio
    async def test_a_failing_refresh_keeps_the_last_good_answer(self):
        class Good:
            async def get_personal_cookbook(self):
                return PAYLOAD

        class Broken:
            async def get_personal_cookbook(self):
                raise RuntimeError("mothership unreachable")

        await pcc.refresh_personal_cookbook(Good())
        pcc.expire_personal_cookbook_cache()
        await pcc.refresh_personal_cookbook(Broken())

        assert "lionhearted-metallic-gold" in pcc.build_personal_cookbook_context()

    @pytest.mark.asyncio
    async def test_a_refresh_returning_none_yields_no_block_and_no_raise(self):
        class Empty:
            async def get_personal_cookbook(self):
                return None

        assert await pcc.refresh_personal_cookbook(Empty()) == []
        assert pcc.build_personal_cookbook_context() == ""


# ---------------------------------------------------------------------------
# ...and the addendum has to reach A TURN, which the first attempt did not (0.7.9).
#
# `build_system_prompt_with_project_context` looked like the right place — it is where
# LEONARDO.md and MEMORY.md go. But for every mode except beginner it runs exactly ONCE,
# at container startup:
#
#   main.py:343         app.state.compiled_graphs = { ... build_rails_agent(...) ... }
#   rails_agent/nodes.py:278   system_prompt=get_cached_system_prompt()
#   -> build_system_prompt_with_project_context(RAILS_AGENT_PROMPT, ...)
#
# The graphs are compiled as singletons at boot, so whatever that call returns is frozen
# for the life of the llamabot process. And the module cache is ALWAYS cold at boot, so
# what it returns is always the empty string. A recipe published five minutes ago would
# not appear until the box restarted — which on a permanent box is weeks. That defeats
# the entire feature: "publish on box A, use it on box B right now" is the point.
#
# The fix is the per-turn pattern this codebase already has: AgentMiddleware, the way
# ViewPathContextMiddleware injects fresh page context on every turn
# (rails_agent/middleware.py:52, wired at nodes.py:262).
#
# rails_beginner_agent is the exception and keeps the project_context wiring: it is a raw
# StateGraph that runs NO middleware, and rebuilds its system message inside the node on
# every turn (nodes.py:182, "Rebuilt every turn" at :53). So it was never frozen.
# ---------------------------------------------------------------------------

from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.leonardo.rails_agent.middleware import (
    PersonalCookbookMiddleware,
    inject_personal_cookbook,
)

MIDDLEWARE_AGENTS = [
    "rails_agent",
    "rails_plan_mode_agent",
    "rails_engineer_plan_mode_agent",
    "rails_ticket_mode_agent",
]


class _Request:
    """Enough of langchain's ModelRequest for these tests."""

    def __init__(self, system_message, messages=None):
        self.system_message = system_message
        self.messages = messages or [HumanMessage(content="hi")]
        self.overrides = {}

    def override(self, **kw):
        self.overrides.update(kw)
        new = _Request(kw.get("system_message", self.system_message), kw.get("messages", self.messages))
        new.overrides = self.overrides
        return new


def _run(request):
    """Drive the middleware synchronously and hand back what the model would see."""
    seen = {}

    def handler(req):
        seen["request"] = req
        return "ok"

    inject_personal_cookbook.wrap_model_call(request, handler)
    return seen["request"]


def _text_of(system_message):
    content = getattr(system_message, "content", system_message)
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict))


# The cached system message rails_agent actually compiles: ONE text block carrying
# Anthropic's ephemeral cache_control (~90% input-token saving).
def _cached_system_message(text="You are Leo. " * 40):
    return SystemMessage(content=[{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}])


class TestTheBlockReachesATurn:
    """The regression that matters. Compile with a COLD cache, publish, then take a turn."""

    def test_a_recipe_published_after_startup_still_reaches_the_model(self):
        # Boot: cache cold, so the compile-time prompt carries no cookbook block.
        compiled = _cached_system_message()
        assert "Personal Cookbook" not in _text_of(compiled)

        # The user publishes on their other Leo; the background refresh lands.
        _prime(PAYLOAD)

        # Next turn.
        out = _run(_Request(compiled))

        assert "lionhearted-metallic-gold" in _text_of(out.system_message)

    def test_the_same_block_appears_on_the_turn_after_that(self):
        _prime(PAYLOAD)
        compiled = _cached_system_message()

        first = _run(_Request(compiled))
        second = _run(_Request(compiled))

        assert "lionhearted-metallic-gold" in _text_of(first.system_message)
        assert "lionhearted-metallic-gold" in _text_of(second.system_message)


class TestItDoesNotBreakWhatItTouches:
    def test_the_cached_block_is_left_byte_identical(self):
        """Mutating the cached block would throw away the prompt cache on every turn."""
        _prime(PAYLOAD)
        compiled = _cached_system_message()
        original = compiled.content[0]

        out = _run(_Request(compiled))

        assert out.system_message.content[0] == original
        assert out.system_message.content[0]["cache_control"] == {"type": "ephemeral"}

    def test_the_cookbook_block_is_added_uncached(self):
        """It changes whenever the user publishes, so caching it would defeat the point."""
        _prime(PAYLOAD)

        out = _run(_Request(_cached_system_message()))

        added = out.system_message.content[-1]
        assert "lionhearted-metallic-gold" in added["text"]
        assert "cache_control" not in added

    def test_a_plain_string_system_message_still_works(self):
        _prime(PAYLOAD)

        out = _run(_Request(SystemMessage(content="You are Leo. " * 40)))

        assert "lionhearted-metallic-gold" in _text_of(out.system_message)

    def test_it_never_injects_twice(self):
        """Beginner mode already appends the block itself; nothing may double it."""
        _prime(PAYLOAD)
        already = SystemMessage(content=pcc.build_personal_cookbook_context())

        out = _run(_Request(already))

        assert _text_of(out.system_message).count("# Your Personal Cookbook") == 1

    def test_an_empty_cookbook_leaves_the_request_completely_alone(self):
        compiled = _cached_system_message()

        out = _run(_Request(compiled))

        assert out.system_message is compiled

    def test_it_never_raises(self):
        with patch.object(pcc, "build_personal_cookbook_context", side_effect=RuntimeError("boom")):
            out = _run(_Request(_cached_system_message()))

        assert out is not None

    def test_it_leaves_the_conversation_untouched(self):
        _prime(PAYLOAD)
        messages = [HumanMessage(content="make the button gold")]

        out = _run(_Request(_cached_system_message(), messages))

        assert out.messages == messages


class TestEveryMiddlewareModeIsWired:
    """A mode left out is a mode where the feature silently does not exist."""

    @pytest.mark.parametrize("agent", MIDDLEWARE_AGENTS)
    def test_the_agent_wires_the_middleware(self, agent):
        source = (AGENTS / agent / "nodes.py").read_text()

        assert "inject_personal_cookbook" in source, (
            f"{agent} never wires inject_personal_cookbook, so its system prompt is still "
            "frozen at the value it had when the graph was compiled at container startup."
        )

    @pytest.mark.parametrize("agent", MIDDLEWARE_AGENTS)
    def test_it_is_in_the_middleware_list_not_merely_imported(self, agent):
        source = (AGENTS / agent / "nodes.py").read_text()

        # Once in the import block, once in the middleware list.
        assert source.count("inject_personal_cookbook") >= 2, (
            f"{agent} imports inject_personal_cookbook but never puts it in the middleware list."
        )

    def test_beginner_mode_is_deliberately_not_wired(self):
        """It runs no middleware at all; its prompt is rebuilt inside the node every turn."""
        source = (AGENTS / "rails_beginner_agent" / "nodes.py").read_text()

        assert "inject_personal_cookbook" not in source
        assert "get_sys_msg()" in source  # rebuilt per turn — see nodes.py:182


class TestTheMiddlewareStillMakesNoNetworkCall:
    def test_a_turn_never_blocks_on_the_mothership(self):
        import httpx

        _prime(PAYLOAD)

        def _boom(*a, **k):
            raise AssertionError("a turn made a blocking HTTP call")

        with patch.object(httpx, "AsyncClient", _boom), patch.object(httpx, "Client", _boom):
            out = _run(_Request(_cached_system_message()))

        assert "lionhearted-metallic-gold" in _text_of(out.system_message)


def test_the_middleware_class_is_exported_as_a_singleton():
    assert isinstance(inject_personal_cookbook, PersonalCookbookMiddleware)
