"""
Tests for batching ask_user_question (0.7.4):
- normalize_questions folds the batch form and the legacy single form into one list
- the 4-question cap truncates loudly (the model is told what was dropped) rather than
  silently, and rather than rendering a wall of questions
- the interrupt payload carries `questions` AND mirrors the first one into the legacy
  top-level fields, so a stale cached frontend still renders a question
- _check_and_send_interrupts forwards `questions` to the frontend

The answer FORMAT (mapping answers back to questions, the per-question visual-options
directive) is the frontend's contract and is covered by tests/js/batched_questions.test.mjs.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import ToolMessage
from langgraph.types import Command


class TestNormalizeQuestions:
    """The pure fold — tested directly so no graph/event loop is needed."""

    def test_batch_form_is_normalized(self):
        from app.agents.leonardo.rails_plan_mode_agent.nodes import normalize_questions

        batch, dropped = normalize_questions([
            {"question": "Which pages?", "options": ["All", "Checkout"]},
            {"question": "What style?", "ui_related": True},
        ])

        assert dropped == 0
        assert batch == [
            {"question": "Which pages?", "options": ["All", "Checkout"], "ui_related": False},
            {"question": "What style?", "options": [], "ui_related": True},
        ]

    def test_legacy_single_question_form_still_works(self):
        """The old flat call signature folds into a one-item batch."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import normalize_questions

        batch, dropped = normalize_questions(None, "Which pages?", ["All"], True)

        assert dropped == 0
        assert batch == [{"question": "Which pages?", "options": ["All"], "ui_related": True}]

    def test_blank_and_malformed_questions_are_dropped(self):
        from app.agents.leonardo.rails_plan_mode_agent.nodes import normalize_questions

        batch, _ = normalize_questions([
            {"question": "Real?"},
            {"question": "   "},
            {"options": ["no question here"]},
            "not a dict",
        ])

        assert [q["question"] for q in batch] == ["Real?"]

    def test_cap_truncates_and_reports_the_drop(self):
        """More than 4 questions is a wall, not a card — cap it but say so."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import (
            normalize_questions, MAX_BATCHED_QUESTIONS,
        )

        batch, dropped = normalize_questions([{"question": f"Q{i}?"} for i in range(7)])

        assert len(batch) == MAX_BATCHED_QUESTIONS
        assert dropped == 7 - MAX_BATCHED_QUESTIONS


class TestAskUserQuestionTool:
    """The tool's interrupt payload."""

    def _runtime(self):
        runtime = MagicMock()
        runtime.tool_call_id = "tool_call_abc"
        return runtime

    def test_batch_interrupt_carries_questions_and_legacy_mirror(self):
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "answers"

            ask_user_question.func(
                runtime=self._runtime(),
                questions=[
                    {"question": "Which pages?", "options": ["All"]},
                    {"question": "What style?", "ui_related": True},
                ],
                context="Phase 1: Clarify",
            )

            payload = mock_interrupt.call_args[0][0]
            assert payload["type"] == "user_question"
            assert len(payload["questions"]) == 2
            assert payload["questions"][1]["ui_related"] is True
            # Legacy mirror of the FIRST question, so an old cached frontend that
            # doesn't know about `questions` still shows something real.
            assert payload["question"] == "Which pages?"
            assert payload["options"] == ["All"]
            assert payload["ui_related"] is False

    def test_legacy_single_call_still_fires_the_same_shape(self):
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "Checkout"

            result = ask_user_question.func(
                runtime=self._runtime(),
                question="Which pages?",
                options=["All", "Checkout"],
                ui_related=True,
            )

            payload = mock_interrupt.call_args[0][0]
            assert payload["question"] == "Which pages?"
            assert payload["ui_related"] is True
            assert payload["questions"] == [
                {"question": "Which pages?", "options": ["All", "Checkout"], "ui_related": True}
            ]

            assert isinstance(result, Command)
            assert "Checkout" in result.update["messages"][0].content

    def test_overflow_note_reaches_the_model(self):
        """Dropped questions must be visible to the agent, or they're silently lost."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            mock_interrupt.return_value = "ok"

            result = ask_user_question.func(
                runtime=self._runtime(),
                questions=[{"question": f"Q{i}?"} for i in range(6)],
            )

            content = result.update["messages"][0].content
            assert "not shown to the user" in content
            assert "next turn" in content

    def test_empty_questions_does_not_hang_the_graph(self):
        """No question at all must return a ToolMessage, never a live interrupt."""
        from app.agents.leonardo.rails_plan_mode_agent.nodes import ask_user_question

        with patch(
            "app.agents.leonardo.rails_plan_mode_agent.nodes.interrupt"
        ) as mock_interrupt:
            result = ask_user_question.func(runtime=self._runtime(), questions=[])

            mock_interrupt.assert_not_called()
            message = result.update["messages"][0]
            assert isinstance(message, ToolMessage)
            assert message.tool_call_id == "tool_call_abc"
            assert "ask_user_question" in message.content


class TestCheckAndSendInterruptsBatched:
    """The WebSocket frame the frontend actually receives."""

    async def _send(self, interrupt_value):
        from app.websocket.request_handler import RequestHandler

        handler = RequestHandler.__new__(RequestHandler)
        handler._connection_locks = {}
        handler._is_websocket_open = MagicMock(return_value=True)

        mock_interrupt = MagicMock()
        mock_interrupt.value = interrupt_value

        mock_task = MagicMock()
        mock_task.interrupts = [mock_interrupt]

        mock_state_snapshot = MagicMock()
        mock_state_snapshot.tasks = [mock_task]

        mock_app = AsyncMock()
        mock_app.aget_state = AsyncMock(return_value=mock_state_snapshot)

        mock_websocket = AsyncMock()
        message_data = {"thread_id": "thread_123", "agent_name": "rails_plan_mode_agent"}

        result = await handler._check_and_send_interrupts(
            mock_app, {}, message_data, mock_websocket
        )
        return result, mock_websocket.send_json.call_args[0][0]

    @pytest.mark.asyncio
    async def test_questions_batch_is_forwarded(self):
        questions = [
            {"question": "Which pages?", "options": ["All"], "ui_related": False},
            {"question": "What style?", "options": [], "ui_related": True},
        ]

        handled, sent = await self._send({
            "type": "user_question",
            "questions": questions,
            "question": "Which pages?",
            "options": ["All"],
            "ui_related": False,
            "context": "Phase 1: Clarify",
        })

        assert handled is True
        assert sent["type"] == "question_request"
        assert sent["questions"] == questions
        assert sent["question"] == "Which pages?"
        assert sent["thread_id"] == "thread_123"

    @pytest.mark.asyncio
    async def test_missing_questions_defaults_to_empty_list(self):
        """An interrupt written before batching existed must still render."""
        handled, sent = await self._send({
            "type": "user_question",
            "question": "Which pages?",
            "options": ["All"],
        })

        assert handled is True
        assert sent["questions"] == []
        assert sent["question"] == "Which pages?"


class TestBatchingSurvivesMothershipPromptOverride:
    """The bug that made batching look broken on a live box.

    The mothership ships its own copy of the plan-mode prompts, and
    resolve_base_prompt prefers it over the constant in prompts.py. That cached copy
    still says "CRITICAL: One question per turn. Do NOT ask multiple questions at
    once." — so editing prompts.py alone changed nothing the model ever saw.

    The batching directive is appended AFTER the resolved base prompt, so it lands
    after a mothership override too. These tests fail if anyone moves it back into
    the constant.
    """

    MODES = [
        ("app.agents.leonardo.rails_plan_mode_agent.nodes", "rails_plan_mode_agent"),
        ("app.agents.leonardo.rails_engineer_plan_mode_agent.nodes", "rails_engineer_plan_mode_agent"),
        ("app.agents.leonardo.rails_ticket_plan_mode_agent.nodes", "rails_ticket_plan_mode_agent"),
    ]

    @pytest.mark.parametrize("module_path,agent_mode", MODES)
    def test_directive_survives_a_stale_mothership_prompt(self, module_path, agent_mode):
        import importlib

        module = importlib.import_module(module_path)

        # A mothership override that actively forbids batching — the real situation.
        stale = (
            "You are Leo.\n\n**CRITICAL: One question per turn.** Do NOT ask multiple "
            "questions at once. Ask one, wait for the answer, then ask the next one.\n"
        ) + ("filler to clear the minimum-length guard. " * 200)

        with patch(
            "app.agents.leonardo.project_context.system_prompt_cache.get_cached",
            return_value=stale,
        ):
            prompt = module.get_cached_system_prompt().content[0]["text"]

        # The stale override really is in play (otherwise this test proves nothing).
        assert "One question per turn" in prompt

        # ...and the batching directive still reaches the model, after it.
        assert "questions` list and shows 2-4 questions on a single card" in prompt
        assert "supersedes any instruction above" in prompt
        assert prompt.index("One question per turn") < prompt.index("supersedes any instruction above")

    @pytest.mark.parametrize("module_path,agent_mode", MODES)
    def test_directive_present_without_an_override(self, module_path, agent_mode):
        import importlib

        module = importlib.import_module(module_path)

        with patch(
            "app.agents.leonardo.project_context.system_prompt_cache.get_cached",
            return_value=None,
        ):
            prompt = module.get_cached_system_prompt().content[0]["text"]

        assert "Default to batching" in prompt
        assert "do not re-ask those" in prompt
