"""LP-43: points every agent that writes Rails views at the accessibility guide.

Appended as a SUFFIX in each agent's get_cached_system_prompt(), after the resolved base
prompt, so it reaches the model even where a mothership prompt override replaces the
agent's prompts.py wholesale. Kept short: it rides on every turn.

The names here (llama-live-region, LlamaA11y.announce, llama_bot_rails/testing/axe) are
defined by the llama_bot_rails gem from 0.7.20. The cookbook guide uses the same names;
change them together.
"""

ACCESSIBLE_VIEWS_DIRECTIVE = """

---

## Accessible views (WCAG 2.1 AA)

Before you write or change a view, follow https://llamapress.ai/cookbook/accessible-views-wcag-aa .

The app already adds a page language, a skip link, a focus ring and a hidden live region
(`#llama-live-region`) to every page, and announces flash notices and form errors. Don't
add your own copies. To announce a status message yourself, call
`window.LlamaA11y.announce("Saved.")` in JS, or from a controller
`turbo_stream.update "llama-live-region", "Saved."`. Never make it play sound.

To prove a screen is accessible, add a system spec like `spec/system/accessibility_spec.rb`:
`require "llama_bot_rails/testing/axe"`, visit the page, then
`expect(page).to be_axe_clean.according_to(*LlamaBotRails::Testing::Axe::WCAG_TAGS)`.
"""
