"""The model-call boundary half of mid-turn Rails auto-recovery.

This middleware runs before every single model call in the product, so the tests
that matter most are the negative ones: with no watch installed, with the feed
broken, or in a read-only mode, it must be indistinguishable from not being
there at all.

See docs/dev/rails_auto_recovery.md.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agents.leonardo.rails_error_watch_middleware import RailsErrorWatchMiddleware
from app.lib.rails_error_watch import (
    ARMING_WINDOW_SECONDS,
    clear_error_watch,
    start_error_watch,
)


class FakeRequest:
    """Stand-in for LangChain's ModelRequest — messages plus override()."""

    def __init__(self, messages):
        self.messages = list(messages)

    def override(self, **kwargs):
        return FakeRequest(kwargs.get("messages", self.messages))


class FakeFeed:
    """Records the cursors it was asked for and replays canned answers."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    async def fetch(self, *, since, within=None):
        self.calls.append((since, within))
        if not self.answers:
            return None
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def middleware_with(feed):
    return RailsErrorWatchMiddleware(feed_factory=lambda watch: feed)


def crash(seq=2, message="undefined method `titl'"):
    return {
        "seq": seq,
        "fingerprint": f"fp-{seq}",
        "error_class": "NoMethodError",
        "message": message,
        "method": "GET",
        "path": "/posts/3",
        "count": 1,
        "backtrace": ["app/views/posts/show.html.erb:4"],
    }


async def run(middleware, request):
    """Call the middleware with a handler that records what it received."""
    seen = {}

    async def handler(req):
        seen["request"] = req
        return "model-response"

    result = await middleware.awrap_model_call(request, handler)
    return seen["request"], result


@pytest.fixture(autouse=True)
def _no_watch_leaks():
    clear_error_watch()
    yield
    clear_error_watch()


@pytest.mark.asyncio
class TestInert:
    async def test_no_watch_means_the_request_is_passed_straight_through(self):
        # Headless runs, tests, and anything that did not go through the chat
        # request handler have no watch installed.
        feed = FakeFeed([(9, [crash()])])
        request = FakeRequest([HumanMessage(content="hi")])

        seen, result = await run(middleware_with(feed), request)

        assert seen is request
        assert result == "model-response"
        assert feed.calls == []

    async def test_a_plan_mode_turn_never_polls(self):
        start_error_watch(thread_id="t", agent_name="rails_plan_mode_agent", api_token="tok")
        feed = FakeFeed([(9, [crash()])])

        seen, _ = await run(middleware_with(feed), FakeRequest([HumanMessage(content="hi")]))

        assert feed.calls == []
        assert len(seen.messages) == 1

    async def test_a_turn_with_no_rails_user_token_still_polls(self):
        # See TestArming in test_rails_error_watch.py: the box reads its own
        # error log, so being signed out of Rails cannot switch this off.
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token=None)
        feed = FakeFeed([(9, [crash()])])

        seen, _ = await run(middleware_with(feed), FakeRequest([HumanMessage(content="hi")]))

        assert feed.calls == [(None, ARMING_WINDOW_SECONDS)]
        assert len(seen.messages) == 2


@pytest.mark.asyncio
class TestPriming:
    async def test_the_first_model_call_arms_the_cursor(self):
        watch = start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, [])])

        seen, _ = await run(middleware_with(feed), FakeRequest([HumanMessage(content="hi")]))

        assert feed.calls == [(None, ARMING_WINDOW_SECONDS)]
        assert watch.cursor == 41
        assert len(seen.messages) == 1

    async def test_an_app_already_broken_when_the_user_asked_is_reported(self):
        # The case the first build missed: user is parked on the error page and
        # types "fix it". No NEW crash is coming, so the arming call has to be
        # the one that speaks up.
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, [crash(seq=41)])])

        seen, _ = await run(middleware_with(feed), FakeRequest([HumanMessage(content="fix it")]))

        assert len(seen.messages) == 2
        assert seen.messages[-1].content.startswith("[automated]")
        assert "RIGHT NOW" in seen.messages[-1].content

    async def test_the_same_crash_is_not_reported_again_after_arming(self):
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, [crash(seq=41)]), (41, [crash(seq=41)])])
        mw = middleware_with(feed)

        await run(mw, FakeRequest([HumanMessage(content="fix it")]))
        seen, _ = await run(mw, FakeRequest([HumanMessage(content="fix it")]))

        assert len(seen.messages) == 1

    async def test_the_second_call_asks_for_what_happened_since(self):
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, []), (41, [])])
        mw = middleware_with(feed)

        await run(mw, FakeRequest([HumanMessage(content="hi")]))
        await run(mw, FakeRequest([HumanMessage(content="hi")]))

        assert feed.calls == [(None, ARMING_WINDOW_SECONDS), (41, None)]


@pytest.mark.asyncio
class TestInjection:
    async def test_a_crash_during_the_turn_is_put_in_front_of_the_model(self):
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, []), (42, [crash(seq=42)])])
        mw = middleware_with(feed)
        await run(mw, FakeRequest([HumanMessage(content="hi")]))

        seen, result = await run(
            mw, FakeRequest([HumanMessage(content="hi"), ToolMessage(content="ok", tool_call_id="1")])
        )

        assert len(seen.messages) == 3
        injected = seen.messages[-1]
        assert isinstance(injected, HumanMessage)
        assert injected.content.startswith("[automated]")
        assert "NoMethodError" in injected.content
        # Transparent: the handler's return value is never touched.
        assert result == "model-response"

    async def test_the_cursor_advances_so_the_same_crash_is_not_refetched(self):
        watch = start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, []), (42, [crash(seq=42)]), (42, [])])
        mw = middleware_with(feed)

        await run(mw, FakeRequest([HumanMessage(content="hi")]))
        await run(mw, FakeRequest([HumanMessage(content="hi")]))
        await run(mw, FakeRequest([HumanMessage(content="hi")]))

        assert feed.calls == [(None, ARMING_WINDOW_SECONDS), (41, None), (42, None)]
        assert watch.cursor == 42

    async def test_nothing_is_appended_after_an_unanswered_tool_call(self):
        # An assistant message with tool_calls must be followed by ToolMessages;
        # slipping a HumanMessage in there 400s the next model call.
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([(41, []), (42, [crash(seq=42)])])
        mw = middleware_with(feed)
        await run(mw, FakeRequest([HumanMessage(content="hi")]))

        pending = AIMessage(
            content="",
            tool_calls=[{"name": "edit_file", "args": {}, "id": "call_1"}],
        )
        seen, _ = await run(mw, FakeRequest([HumanMessage(content="hi"), pending]))

        assert len(seen.messages) == 2


@pytest.mark.asyncio
class TestNeverFatal:
    async def test_a_raising_feed_does_not_break_the_turn(self):
        start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([RuntimeError("feed exploded")])
        request = FakeRequest([HumanMessage(content="hi")])

        seen, result = await run(middleware_with(feed), request)

        assert seen is request
        assert result == "model-response"

    async def test_an_unreachable_feed_leaves_the_cursor_unarmed(self):
        watch = start_error_watch(thread_id="t", agent_name="rails_agent", api_token="tok")
        feed = FakeFeed([None, (41, [])])
        mw = middleware_with(feed)

        await run(mw, FakeRequest([HumanMessage(content="hi")]))
        assert watch.cursor is None

        # Rails comes back; the next call arms as if it were the first.
        await run(mw, FakeRequest([HumanMessage(content="hi")]))
        assert watch.cursor == 41
        assert feed.calls == [(None, ARMING_WINDOW_SECONDS), (None, ARMING_WINDOW_SECONDS)]


def test_every_leonardo_agent_gets_the_middleware(monkeypatch):
    # Wiring guard, same chokepoint argument as TurnMetricsMiddleware: if it is
    # not added here, individual modes lose auto-recovery silently.
    from app.agents.leonardo import agent_factory

    captured = {}

    def fake_create_agent(**kwargs):
        captured["middleware"] = kwargs.get("middleware")
        return "agent"

    monkeypatch.setattr(agent_factory, "create_agent", fake_create_agent)
    agent_factory.build_leonardo_agent(model="x", tools=[])

    assert sum(
        isinstance(m, RailsErrorWatchMiddleware) for m in captured["middleware"]
    ) == 1


def test_the_middleware_is_not_added_twice(monkeypatch):
    from app.agents.leonardo import agent_factory

    captured = {}

    def fake_create_agent(**kwargs):
        captured["middleware"] = kwargs.get("middleware")
        return "agent"

    monkeypatch.setattr(agent_factory, "create_agent", fake_create_agent)
    agent_factory.build_leonardo_agent(
        model="x", tools=[], middleware=[RailsErrorWatchMiddleware()]
    )

    assert sum(
        isinstance(m, RailsErrorWatchMiddleware) for m in captured["middleware"]
    ) == 1


def test_the_request_handler_arms_and_resumes_the_watch():
    """Wiring guard for the three call sites in the WebSocket request handler.

    Source-level, same approach as the build_leonardo_agent guard in
    test_orphaned_toolcall_repair_all_agents.py: standing the whole handler up
    would test FastAPI, not this. What must hold is that a user message starts a
    fresh watch and that BOTH resume paths re-install it — the browser_command
    resume in particular, since Leo navigating the preview to verify its own fix
    is the most likely moment for a 500 to appear.
    """
    from pathlib import Path

    import app.websocket.request_handler as rh

    source = Path(rh.__file__).read_text()

    assert source.count("start_error_watch(") == 1, "exactly one arm site"
    assert source.count("resume_error_watch(thread_id=") == 2, (
        "both handle_approval_response and handle_question_response must resume "
        "the turn's watch"
    )
