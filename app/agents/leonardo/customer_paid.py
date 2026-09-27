"""Customer-paid turns: run on the user's own ChatGPT plan or not at all (0.7.11).

Two ways a turn becomes customer-paid:

1. **The box is customer-paid only.** ``customer_paid_only: true`` in the pushed
   model policy (fleet or ``instance_overrides``). Darren's Base plan: hosting only,
   no LlamaPress AI, every turn on the customer's own ChatGPT account.
2. **The paywall let it through.** A customer who is out of messages or spend may
   always keep going on their own ChatGPT account (Darren, 2026-09-23). The
   websocket handler marks that one turn with :func:`start_customer_paid_turn`.

Either way the rule is the same: never spend OUR keys. Before this, a missing
ChatGPT credential fell back to the box default (DeepSeek on our key), and titles
and the summarizer's fallback chain called our vendors directly. On a customer-paid
turn a missing credential raises :class:`ChatGPTNotConnected` instead, which the
chat shows as a plain "connect your account" message.
"""

import logging
from contextvars import ContextVar

from langchain_core.language_models.chat_models import BaseChatModel

logger = logging.getLogger(__name__)

#: What the user sees. Plain on purpose; the frame also carries a connect action.
CONNECT_MESSAGE = "Your ChatGPT account is not connected. Connect it to keep working."

_turn_customer_paid: ContextVar[bool] = ContextVar(
    "llamabot_turn_customer_paid", default=False
)


class ChatGPTNotConnected(RuntimeError):
    """A customer-paid turn with no usable ChatGPT credential."""

    def __init__(self, message: str = CONNECT_MESSAGE):
        super().__init__(message)


def start_customer_paid_turn() -> object:
    """Mark the current turn customer-paid. Tasks spawned from it inherit the mark."""
    return _turn_customer_paid.set(True)


def turn_is_customer_paid() -> bool:
    return _turn_customer_paid.get()


def policy_enabled() -> bool:
    """True when the pushed model policy makes this box customer-paid only. Never raises."""
    try:
        from app.agents.leonardo.model_policy import remote_policy

        return remote_policy().get("customer_paid_only") is True
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not read customer_paid_only from the model policy: %s", e)
        return False


def required() -> bool:
    """True when the current call must not reach any platform-paid model."""
    return turn_is_customer_paid() or policy_enabled()


def chatgpt_models() -> tuple:
    from app.agents.leonardo.llm_factory import _CHATGPT_SUBSCRIPTION_MODELS

    return tuple(_CHATGPT_SUBSCRIPTION_MODELS)


def is_chatgpt_model(model_name) -> bool:
    return model_name in chatgpt_models()


def default_chatgpt_model() -> str:
    """The ChatGPT model a customer-paid call lands on when it asked for another one."""
    from app.agents.leonardo.model_policy import is_model_enabled

    models = chatgpt_models()
    for name in models:
        if is_model_enabled(name):
            return name
    return models[0]


class NotConnectedChatModel(BaseChatModel):
    """Stand-in for a customer-paid ChatGPT model with no credential behind it.

    Returned instead of raising so graphs still compile (they build a model with no
    user in context); every call raises :class:`ChatGPTNotConnected`.
    """

    @property
    def _llm_type(self) -> str:
        return "chatgpt-not-connected"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        raise ChatGPTNotConnected()

    async def _agenerate(self, *args, **kwargs):
        raise ChatGPTNotConnected()
