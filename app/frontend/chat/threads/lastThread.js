/**
 * The chat the user was last in, so a reload or a trip to another page brings
 * them back to it instead of a blank chat. localStorage is per origin, so this
 * is already per box. Storage can be blocked; every call fails quietly.
 */

const KEY = 'llamabot:lastThreadId';

export function rememberThread(threadId) {
  if (!threadId) return;
  try { localStorage.setItem(KEY, threadId); } catch (e) { /* ignore */ }
}

export function recallThread() {
  try { return localStorage.getItem(KEY) || null; } catch (e) { return null; }
}

/** URL params that start their own chat, and so beat the remembered one. */
export function startsFreshChat(params) {
  return ['prompt', 'welcome_prompt', 'conversation'].some((p) => params.has(p));
}
