"""Per-turn bookkeeping for mid-turn Rails auto-recovery.

Pure logic only: no LangGraph, no HTTP, no event loop. Everything that decides
whether an error is injected — and everything that stops a fix/break/fix loop —
lives here so it can be pinned without a model.

See docs/dev/rails_auto_recovery.md.
"""
import pytest

from app.lib.rails_error_watch import (
    MAX_INJECTIONS_PER_TURN,
    MAX_REPORT_CHARS,
    RailsErrorWatch,
    agent_can_auto_recover,
)


def error(
    seq=1,
    fingerprint=None,
    error_class="NoMethodError",
    message="undefined method `titl' for an instance of Post",
    path="/posts/3",
    backtrace=None,
    count=1,
):
    return {
        "seq": seq,
        "fingerprint": fingerprint or f"fp-{error_class}-{message}-{path}",
        "error_class": error_class,
        "message": message,
        "method": "GET",
        "path": path,
        "context": "rack_middleware",
        "count": count,
        "at": "2026-08-22T10:00:00Z",
        "backtrace": backtrace if backtrace is not None else ["app/views/posts/show.html.erb:4"],
    }


def watch(agent_name="rails_agent", api_token="tok"):
    return RailsErrorWatch(thread_id="t1", agent_name=agent_name, api_token=api_token)


class TestArming:
    def test_a_working_agent_with_a_token_is_armed(self):
        assert watch().armed is True

    def test_a_watch_without_an_api_token_is_still_armed(self):
        # The regression that broke this feature in the field: arming used to
        # require the per-user Rails token off the WebSocket frame, which only
        # exists while the human holds a Devise session. The box authenticates
        # to its own error log with a derived secret instead, so a signed-out
        # user must NOT disable crash recovery.
        assert watch(api_token=None).armed is True

    def test_plan_mode_agents_cannot_auto_recover(self):
        assert agent_can_auto_recover("rails_plan_mode_agent") is False
        assert agent_can_auto_recover("rails_engineer_plan_mode_agent") is False
        assert watch(agent_name="rails_plan_mode_agent").armed is False

    def test_editing_agents_can_auto_recover(self):
        assert agent_can_auto_recover("rails_agent") is True
        assert agent_can_auto_recover("rails_beginner_agent") is True

    def test_missing_agent_name_cannot_auto_recover(self):
        assert agent_can_auto_recover(None) is False
        assert agent_can_auto_recover("") is False


class TestPlanInjection:
    def test_no_errors_means_no_message(self):
        assert watch().plan_injection([]) is None

    def test_a_new_error_produces_a_report_the_model_can_act_on(self):
        text = watch().plan_injection([error()])

        assert text is not None
        assert text.startswith("[automated]")
        assert "NoMethodError" in text
        assert "undefined method `titl'" in text
        assert "/posts/3" in text
        assert "app/views/posts/show.html.erb:4" in text

    def test_the_same_error_is_only_reported_once_per_turn(self):
        w = watch()
        assert w.plan_injection([error(seq=1)]) is not None
        # A render loop re-raises the identical error; the model already knows.
        assert w.plan_injection([error(seq=2)]) is None
        assert w.injections == 1

    def test_two_errors_in_one_poll_become_one_message(self):
        text = watch().plan_injection([
            error(seq=1, error_class="NoMethodError", message="one"),
            error(seq=2, error_class="TypeError", message="two"),
        ])

        assert text.count("[automated]") == 1
        assert "NoMethodError" in text and "TypeError" in text

    def test_a_repeat_count_is_surfaced(self):
        text = watch().plan_injection([error(count=7)])
        assert "7" in text

    def test_the_budget_allows_three_attempts(self):
        w = watch()
        for i in range(MAX_INJECTIONS_PER_TURN):
            assert w.plan_injection([error(seq=i, message=f"boom {i}")]) is not None
        assert w.injections == MAX_INJECTIONS_PER_TURN

    def test_the_fourth_error_tells_the_agent_to_stop_and_explain(self):
        w = watch()
        for i in range(MAX_INJECTIONS_PER_TURN):
            w.plan_injection([error(seq=i, message=f"boom {i}")])

        text = w.plan_injection([error(seq=99, message="still broken")])

        assert text is not None
        assert "stop" in text.lower()
        # The give-up note is not a repair attempt; the budget stays spent.
        assert w.injections == MAX_INJECTIONS_PER_TURN
        assert w.gave_up is True

    def test_nothing_is_injected_after_the_give_up_note(self):
        w = watch()
        for i in range(MAX_INJECTIONS_PER_TURN):
            w.plan_injection([error(seq=i, message=f"boom {i}")])
        w.plan_injection([error(seq=98, message="give up now")])

        assert w.plan_injection([error(seq=99, message="and another")]) is None

    def test_a_huge_backtrace_cannot_blow_the_context_window(self):
        giant = [f"app/models/thing.rb:{i}:in `method_{i}'" for i in range(400)]
        text = watch().plan_injection([error(backtrace=giant)])

        assert len(text) <= MAX_REPORT_CHARS
        # The instruction survives truncation — a bare stack trace with no ask
        # is the one shape that reliably produces no action.
        assert "reload" in text.lower()

    def test_many_errors_at_once_cannot_blow_the_context_window(self):
        errors = [error(seq=i, message=f"distinct failure number {i}" * 20) for i in range(50)]
        assert len(watch().plan_injection(errors)) <= MAX_REPORT_CHARS

    def test_a_malformed_entry_does_not_raise(self):
        # The feed is JSON off another process; a missing key must not take the
        # turn down with it.
        text = watch().plan_injection([{"seq": 1, "fingerprint": "x"}])
        assert text is None or isinstance(text, str)


class TestCursor:
    def test_a_fresh_watch_has_no_cursor(self):
        assert watch().cursor is None

    def test_priming_records_where_the_log_was_when_the_turn_started(self):
        w = watch()
        w.prime(41)
        assert w.cursor == 41

    def test_priming_twice_does_not_move_the_cursor_backwards(self):
        w = watch()
        w.prime(41)
        w.prime(3)
        assert w.cursor == 41

    def test_advance_moves_the_cursor_forward(self):
        w = watch()
        w.prime(41)
        w.advance(45)
        assert w.cursor == 45


@pytest.mark.parametrize("agent", ["rails_agent", "leonardo"])
def test_context_var_round_trip(agent):
    from app.lib.rails_error_watch import current_error_watch, start_error_watch

    started = start_error_watch(thread_id="t9", agent_name=agent, api_token="tok")
    assert current_error_watch() is started
    assert started.thread_id == "t9"


class TestRegistry:
    """A turn is not one WebSocket message.

    Leo's own verify step is a ``browser_command`` interrupt: the graph pauses,
    the frontend navigates the preview, and the answer arrives as a SEPARATE
    frame that resumes the run. That navigation is the single most likely moment
    for a 500 to appear — so the watch, its cursor and its budget have to
    survive the resume rather than starting over (or vanishing).
    """

    def setup_method(self):
        from app.lib.rails_error_watch import _reset_registry

        _reset_registry()

    def test_a_resume_finds_the_watch_the_turn_started_with(self):
        from app.lib.rails_error_watch import resume_error_watch, start_error_watch

        started = start_error_watch(thread_id="t1", agent_name="rails_agent", api_token="tok")
        started.prime(41)
        started.plan_injection([error(seq=42, message="first crash")])

        resumed = resume_error_watch(thread_id="t1")

        assert resumed is started
        assert resumed.cursor == 41
        # Budget carries across the resume — three attempts per turn, not three
        # per WebSocket frame.
        assert resumed.injections == 1

    def test_resuming_an_unknown_thread_is_not_an_error(self):
        from app.lib.rails_error_watch import current_error_watch, resume_error_watch

        assert resume_error_watch(thread_id="never-seen") is None
        assert current_error_watch() is None

    def test_a_new_user_message_starts_a_fresh_budget(self):
        from app.lib.rails_error_watch import start_error_watch

        first = start_error_watch(thread_id="t1", agent_name="rails_agent", api_token="tok")
        first.plan_injection([error(message="crash from the previous turn")])

        second = start_error_watch(thread_id="t1", agent_name="rails_agent", api_token="tok")

        assert second is not first
        assert second.injections == 0
        assert second.cursor is None

    def test_threads_do_not_share_a_watch(self):
        from app.lib.rails_error_watch import resume_error_watch, start_error_watch

        a = start_error_watch(thread_id="a", agent_name="rails_agent", api_token="tok")
        b = start_error_watch(thread_id="b", agent_name="rails_agent", api_token="tok")

        assert resume_error_watch(thread_id="a") is a
        assert resume_error_watch(thread_id="b") is b

    def test_the_registry_cannot_grow_without_bound(self):
        from app.lib.rails_error_watch import (
            MAX_TRACKED_THREADS,
            _registry_size,
            start_error_watch,
        )

        for i in range(MAX_TRACKED_THREADS + 20):
            start_error_watch(thread_id=f"t{i}", agent_name="rails_agent", api_token="tok")

        assert _registry_size() <= MAX_TRACKED_THREADS

    def test_the_oldest_thread_is_the_one_evicted(self):
        from app.lib.rails_error_watch import (
            MAX_TRACKED_THREADS,
            resume_error_watch,
            start_error_watch,
        )

        for i in range(MAX_TRACKED_THREADS + 1):
            start_error_watch(thread_id=f"t{i}", agent_name="rails_agent", api_token="tok")

        assert resume_error_watch(thread_id="t0") is None
        assert resume_error_watch(thread_id=f"t{MAX_TRACKED_THREADS}") is not None


class TestPreExistingBreakage:
    """The app was already broken when the user hit send.

    This is the common shape — "it's showing an error, fix it" — and the first
    build missed it entirely: the cursor was primed to NOW, so a crash that had
    already happened could never be newer than the cursor and the turn stayed
    silent while the user stared at the error page.
    """

    def test_a_pre_existing_crash_is_reported(self):
        text = watch().plan_injection([error()], pre_existing=True)

        assert text is not None
        assert "NoMethodError" in text

    def test_it_does_not_blame_the_agent_for_a_pre_existing_crash(self):
        text = watch().plan_injection([error()], pre_existing=True)

        # The agent has not done anything yet on this turn; telling it "you
        # probably caused this" invites it to revert work it never did.
        assert "caused by a change you just made" not in text
        assert "right now" in text.lower()

    def test_a_mid_turn_crash_still_points_at_the_agent(self):
        text = watch().plan_injection([error()])

        assert "caused by a change you just made" in text

    def test_a_pre_existing_crash_spends_the_same_budget(self):
        w = watch()
        w.plan_injection([error(message="already broken")], pre_existing=True)

        assert w.injections == 1

    def test_a_pre_existing_crash_is_not_re_reported_mid_turn(self):
        w = watch()
        w.plan_injection([error()], pre_existing=True)

        assert w.plan_injection([error()]) is None
