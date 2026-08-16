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
