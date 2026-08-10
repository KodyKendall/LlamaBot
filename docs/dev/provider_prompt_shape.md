# Provider prompt shape: Anthropic cache blocks vs. strict OpenAI-compatible gateways

**Rule:** never hand a system message with **list** content to a non-Anthropic model.
Route it through `system_message_for_model(...)` in `app/agents/leonardo/llm_factory.py`.

## What broke

Every Leonardo agent builds its system prompt in Anthropic's prompt-caching shape:

```python
SystemMessage(content=[{"type": "text", "text": prompt,
                        "cache_control": {"type": "ephemeral"}}])
```

Anthropic requires that list (it's where `cache_control` lives, worth ~90% of input
token cost). **Fireworks and GMI** — the two OpenAI-compatible gateways we serve
`deepseek-v4-flash` through — reject it: their API requires system `content` to be a
plain string and 400s the turn otherwise. DeepSeek direct and Gemini tolerate the
list, which is why nothing surfaced until the fleet default was policy-remapped to
`deepseek-v4-flash-fireworks` (see `model_policy`) and Leo's **Database Mode**
(`rails_user_mode_agent`) started 400ing on every message.

This is the content-block sibling of the earlier `cache_control=` **invoke kwarg**
crash (`TypeError: Completions.create() got an unexpected keyword argument
'cache_control'`). Same shape of bug, same rule: one predicate, one helper, no
literal `startswith("claude")` at any call site.

## The fix

`system_message_for_model(system_message, model_name)` returns the message untouched
for Anthropic (caching preserved) and flattens the blocks to their concatenated text
for everything else. It accepts a `SystemMessage`, a raw `{"role": "system"}` dict
(what the raw StateGraph nodes build), a plain string, or `None`.

Two classes of call site, both covered:

| Agent shape | Where the flatten happens |
| --- | --- |
| `create_agent` + `DynamicModelMiddleware` (rails, plan, engineer plan, **database/user mode**, user feedback, ticket, ticket plan, testing, pyxl) | `DynamicModelMiddleware._override` — same place the model is selected, sync **and** async paths |
| No middleware: raw StateGraph nodes (`rails_ai_builder_agent`, `rails_beginner_agent`, `rails_plain_chat_mode`) and the sub-agent factories in `rails_agent/sub_agents.py` | at the call site, using the turn's `llm_model` |

## Guardrail

`app/tests/test_system_prompt_blocks_non_anthropic.py` pins the behavior and sweeps
the agent tree: any module that builds a `"cache_control"` system block and does
**not** run `DynamicModelMiddleware` must reference `system_message_for_model`. A new
raw agent that copy-pastes the cached-prompt idiom fails that test instead of failing
on a customer's box.
