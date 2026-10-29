/**
 * One fetch of the per-user Rails API token, plus WHY there isn't one.
 *
 * railsAuth goes on the WS frame as `rails_auth` so agents can tell a signed-out
 * user to sign in, instead of reporting a missing token as a setup problem (LP-23):
 *   'signed_in'  — got a token
 *   'signed_out' — 401: no Rails session
 *   'error'      — network/CORS/5xx
 *
 * Never throws: a failed fetch must never block sending a message.
 */
export async function fetchRailsApiToken(railsUrl, fetchFn = fetch) {
  try {
    const resp = await fetchFn(`${railsUrl}/llama_bot/agent/token`, {
      // Sends the Rails session cookie. Both hosts sit under the same site, so
      // the cookie rides along; Rails must allow this origin + credentials (see
      // AgentTokenController#set_cors_headers) or the browser blocks the read.
      credentials: 'include',
      headers: { 'Accept': 'application/json' }
    });
    if (!resp.ok) {
      // 401 = not signed into Rails. Expected; don't spam the console.
      if (resp.status === 401) return { token: null, railsAuth: 'signed_out' };
      console.warn(`[llamabot] token fetch failed: HTTP ${resp.status}`);
      return { token: null, railsAuth: 'error' };
    }
    const data = await resp.json();
    const token = data.api_token || null;
    return {
      token,
      expiresIn: data.expires_in || 1800,
      railsAuth: token ? 'signed_in' : 'error',
    };
  } catch (e) {
    console.warn('[llamabot] token fetch error:', e.message);
    return { token: null, railsAuth: 'error' };
  }
}
