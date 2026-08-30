/**
 * Re-sync an in-flight assistant reply after the chat socket reconnects.
 *
 * The failure this exists for (rsb-dev, 2026-08-28, thread 1787943690689-qzr1efuar):
 * the socket dropped ~2.5 minutes into a run. ActionCable reconnected the TRANSPORT, but
 * nothing replayed the RUN. The server went on streaming into a socket nobody was reading,
 * and the chunks that arrived after the reconnect were appended to a fresh bubble — so the
 * customer's final answer began mid-sentence, showing exactly the last 435 characters of a
 * complete 906-character reply. Leo's work was correct and fully persisted; only the view
 * broke, and the customer had no way to tell. They paid a message to type "continue", then
 * hard-reloaded the page.
 *
 * Fleet scale: 45 of the 57 boxes capable of reporting it have hit it (78.9%), 122 events,
 * half of them on beginner-mode users — the people least able to tell a truncated answer
 * from a wrong one. Only 1 of 122 produced a thumbs-down, so complaint volume says nothing.
 *
 * Reconnecting the pipe does not recover the turn. These helpers are deliberately pure so
 * the decisions can be tested without a socket, a DOM or a server.
 */

/**
 * Should a reconnect trigger a re-sync?
 *
 * Only when a run was actually in flight when the socket died. An idle reconnect must not
 * refetch and repaint the thread underneath the user.
 */
export function shouldResumeAfterReconnect({ wasRunInFlight, threadId } = {}) {
  return Boolean(wasRunInFlight && threadId);
}

/**
 * Choose the authoritative assistant text from a `/chat-history/{thread_id}` message list.
 *
 * Prefers the message the partial bubble is the TAIL of: that is the exact signature of
 * this bug (in the incident, `full.endsWith(partial)` was true at offset 471), so it
 * identifies the interrupted message rather than merely the newest one. Falls back to the
 * last assistant message, which is still strictly better than leaving a mid-sentence
 * bubble on screen.
 *
 * Returns null rather than guessing when there is no assistant message to show.
 */
export function pickAuthoritativeMessage(messages, { partialText = '' } = {}) {
  if (!Array.isArray(messages) || messages.length === 0) return null;

  // Tool output must never be rendered as Leo speaking, and a human turn is not an answer.
  const assistant = messages.filter(
    (m) => m && (m.type === 'ai' || m.type === 'assistant') && typeof m.content === 'string',
  );
  if (assistant.length === 0) return null;

  const fragment = (partialText || '').trim();
  if (fragment) {
    // Walk newest-first: on a resumed thread the same tail can appear more than once.
    for (let i = assistant.length - 1; i >= 0; i -= 1) {
      const content = assistant[i].content;
      if (content.length > fragment.length && content.trimEnd().endsWith(fragment)) {
        return content;
      }
    }
  }

  return assistant[assistant.length - 1].content;
}

/**
 * Monotonic token for a resume attempt.
 *
 * The history fetch is async and chunks may still be arriving, so a slow response from an
 * older reconnect must never overwrite a newer, correct render. Callers bump the
 * generation when they start a resume and drop any result that is no longer current.
 */
export function nextResumeGeneration(current) {
  return (Number(current) || 0) + 1;
}

/** True when a resume result belongs to a superseded attempt and must be discarded. */
export function isStaleResume(generation, currentGeneration) {
  return Number(generation) !== Number(currentGeneration);
}

/**
 * Fetch the thread state and hand back the authoritative text.
 *
 * Fail-open: any error resolves to null, and the caller leaves the existing bubble alone.
 * A resume that throws must never be worse than the truncation it is fixing.
 */
export async function fetchAuthoritativeMessage(threadId, { partialText = '', fetchImpl } = {}) {
  const doFetch = fetchImpl || (typeof fetch === 'function' ? fetch : null);
  if (!doFetch || !threadId) return null;

  try {
    const response = await doFetch(`/chat-history/${threadId}`);
    if (!response || !response.ok) return null;

    const data = await response.json();
    const messages = data?.messages || data?.values?.messages || null;
    return pickAuthoritativeMessage(messages, { partialText });
  } catch (error) {
    console.warn('Stream resume: could not fetch thread state', error);
    return null;
  }
}
