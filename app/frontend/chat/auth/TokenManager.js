/**
 * Token Manager for WebSocket Authentication
 *
 * Handles fetching, caching, and refreshing JWT tokens for WebSocket auth.
 * Tokens are stored in localStorage and automatically refreshed before expiry.
 */

export class TokenManager {
  static TOKEN_KEY = 'llamabot_ws_token';
  static TOKEN_EXPIRY_KEY = 'llamabot_ws_token_expiry';

  // Buffer time before expiry to refresh token (5 minutes)
  static EXPIRY_BUFFER_MS = 5 * 60 * 1000;

  /**
   * Get a valid token, fetching a new one if needed.
   *
   * @returns {Promise<string|null>} JWT token or null if fetch fails
   */
  static async getToken() {
    // Check if we have a valid cached token
    const cachedToken = this.getCachedToken();
    if (cachedToken) {
      return cachedToken;
    }

    // Fetch new token from API
    return await this.fetchNewToken();
  }

  /**
   * What is known about the token last handed out, for the sign-in failure report
   * (LP-30). Never the token itself.
   * { source: 'network' | 'localStorage', iat, exp, status, error }
   */
  static lastToken = null;

  /**
   * The JWT's claims, or null if it can't be read. The payload is base64url, which
   * atob does not accept until - and _ are mapped back and the padding restored.
   */
  static decodeClaims(token) {
    try {
      const part = String(token).split('.')[1];
      if (!part) return null;
      let b64 = part.replace(/-/g, '+').replace(/_/g, '/');
      while (b64.length % 4) b64 += '=';
      const claims = JSON.parse(atob(b64));
      return claims && typeof claims === 'object' ? claims : null;
    } catch (_e) {
      return null;
    }
  }

  static describe(token, source, extra = {}) {
    const claims = token ? this.decodeClaims(token) : null;
    const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);
    return { source, iat: num(claims?.iat), exp: num(claims?.exp), ...extra };
  }

  /**
   * Get cached token if still valid.
   *
   * Judged by the token's own exp claim (minus the 5-minute buffer) as well as the
   * stored expiry. A token that can't be decoded, or has no numeric exp, counts as
   * expired (LP-30).
   *
   * @returns {string|null} Cached token or null if expired/missing
   */
  static getCachedToken() {
    try {
      const token = localStorage.getItem(this.TOKEN_KEY);
      const expiry = localStorage.getItem(this.TOKEN_EXPIRY_KEY);

      if (!token || !expiry) {
        return null;
      }

      const now = Date.now();
      const storedExpiry = parseInt(expiry, 10);
      const exp = this.decodeClaims(token)?.exp;
      const expValid = typeof exp === 'number' && Number.isFinite(exp) &&
        exp * 1000 - this.EXPIRY_BUFFER_MS > now;

      if (expValid && now < storedExpiry) {
        this.lastToken = this.describe(token, 'localStorage');
        return token;
      }

      // Token expired or unreadable, clear it
      this.clearToken();
      return null;
    } catch (error) {
      console.warn('Error reading cached token:', error);
      return null;
    }
  }

  /**
   * Fetch a new token from the API.
   *
   * Always from the server: cache 'no-store' keeps the browser from answering with a
   * stored response. A tab restored after a discard loads like a history navigation,
   * where Chrome serves stored responses without checking them, so the retry used to
   * get the same expired token back (LP-30).
   *
   * @returns {Promise<string|null>} New token or null if fetch fails
   */
  static async fetchNewToken() {
    let failure = null;
    try {
      const response = await fetch('/api/ws-token', { cache: 'no-store' });

      if (!response.ok) {
        failure = { source: 'network', iat: null, exp: null, status: response.status };
        this.lastToken = failure;
        // 401 means not authenticated - user needs to log in
        if (response.status === 401) {
          console.warn('Not authenticated - cannot get WebSocket token');
          return null;
        }
        throw new Error(`Failed to get token: ${response.status}`);
      }

      const data = await response.json();

      if (!data.token) {
        failure = { source: 'network', iat: null, exp: null, status: response.status, error: 'no token in response' };
        throw new Error('No token in response');
      }

      // Calculate expiry time (with buffer before actual expiry)
      const expiresIn = data.expires_in || 1800; // Default 30 minutes
      const expiresAt = Date.now() + (expiresIn * 1000) - this.EXPIRY_BUFFER_MS;

      // Cache the token
      localStorage.setItem(this.TOKEN_KEY, data.token);
      localStorage.setItem(this.TOKEN_EXPIRY_KEY, expiresAt.toString());
      this.lastToken = this.describe(data.token, 'network', { status: response.status });

      console.log('WebSocket token fetched, expires in', Math.round((expiresAt - Date.now()) / 1000 / 60), 'minutes');

      return data.token;
    } catch (error) {
      this.lastToken = failure || { source: 'network', iat: null, exp: null, error: 'request failed' };
      console.error('Failed to fetch WebSocket token:', error);
      return null;
    }
  }

  /**
   * Clear the cached token.
   * Call this when auth fails or user logs out.
   */
  static clearToken() {
    try {
      localStorage.removeItem(this.TOKEN_KEY);
      localStorage.removeItem(this.TOKEN_EXPIRY_KEY);
    } catch (error) {
      console.warn('Error clearing token:', error);
    }
  }

  /**
   * Check if we have any cached token (valid or not).
   * Useful for deciding whether to attempt auth.
   *
   * @returns {boolean} True if a token exists in cache
   */
  static hasToken() {
    try {
      return !!localStorage.getItem(this.TOKEN_KEY);
    } catch (error) {
      return false;
    }
  }
}
