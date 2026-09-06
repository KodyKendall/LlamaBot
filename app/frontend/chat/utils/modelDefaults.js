/**
 * Which model the dropdown starts on when the user has not chosen one.
 *
 * The default model is box-dependent since 0.7.0 (Muse where the box has a META
 * key, DeepSeek where it does not), so it is resolved server-side by
 * model_policy and reported as `default_model` on /api/available-models. The
 * frontend must FOLLOW that value — a compile-time default baked into
 * chat.html/index.js silently disagrees with the server the moment
 * DEFAULT_LLM_MODEL changes, which is exactly how 0.7.0 shipped every chat turn
 * to DeepSeek while the server default said Muse.
 *
 * Kept as a standalone pure function so the decision can be tested without
 * standing up the whole ChatApp (importing index.js pulls in the entire UI).
 *
 * @param {{value: string, disabled: boolean}[]} options  The dropdown's options,
 *   after availability has been applied. The empty-valued placeholder option is
 *   ignored — it exists only so nothing is pre-selected before the fetch lands.
 * @param {?string} defaultModel  The server's resolved `default_model`.
 * @returns {?string} The value to select, or null when nothing is selectable.
 */
export function chooseInitialModel({ options, defaultModel }) {
  const selectable = (options || []).filter(o => o.value && !o.disabled);
  if (selectable.length === 0) return null;
  // The server default wins, but only when this box can actually run it: on a
  // box whose default model has no API key, /api/available-models reports it
  // `available: false` and the user gets the first model that does work (the
  // DeepSeek-fallback box experience).
  const wanted = selectable.find(o => o.value === defaultModel);
  return (wanted || selectable[0]).value;
}

/**
 * Whether a REMEMBERED model choice can be applied to the dropdown yet.
 *
 * A user's own choice arrives before the dropdown is complete. chat.html carries
 * a static <option> only for registry models; config-registered models
 * (OpenRouter endpoints) are injected at runtime by addMissingModelOptions()
 * inside fetchAvailableModels(). Both startup paths that read a remembered
 * choice — the llmModel cookie and the ?llm_model= pin — run before that
 * injection, so validating there discards every config-registered choice. On a
 * box whose selectable models are all config-registered that is 100% of choices,
 * and the user is returned to the fleet default on every page load (0.7.7).
 *
 * So the startup paths record the choice as INTENT and this decides, once the
 * options exist. Kept as a standalone pure function for the same reason as
 * chooseInitialModel: the decision is testable without standing up the ChatApp.
 *
 * @param {{value: string, disabled?: boolean}[]} options  The dropdown's options,
 *   AFTER addMissingModelOptions() and BEFORE availability is applied.
 * @param {?string} remembered  The cookie's model, or the ?llm_model= pin.
 * @returns {{select: ?string, userChoseModel: boolean}} `select` is the value to
 *   put on the dropdown, or null to leave it alone. `userChoseModel` is false
 *   when the choice cannot be honoured, which lets the server default apply.
 */
export function resolveRememberedModel({ options, remembered }) {
  const none = { select: null, userChoseModel: false };
  if (!remembered) return none;
  // A DISABLED option still counts as chosen. Availability is applied after this
  // runs and the needsNewSelection repair already owns that case: it moves to the
  // first available model, leaves the llmModel cookie intact and raises
  // showModelSubstitutionNotice(). Discarding the choice here would destroy the
  // user's pick on a box that is only temporarily missing a key.
  const match = (options || []).some(o => o.value === remembered);
  // No option at all means this build cannot run the id — a model removed from
  // the registry. Pinning it would leave the dropdown on a value setModel()
  // silently no-ops on, so the server default takes over instead.
  return match ? { select: remembered, userChoseModel: true } : none;
}
