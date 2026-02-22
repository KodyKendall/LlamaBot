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
   * Get cached token if still valid.
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

      const expiryTime = parseInt(expiry, 10);
      const now = Date.now();

      // Check if token is still valid (with buffer)
      if (now < expiryTime) {
        return token;
      }

      // Token expired, clear it
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
   * @returns {Promise<string|null>} New token or null if fetch fails
   */
  static async fetchNewToken() {
    try {
      const response = await fetch('/api/ws-token');

      if (!response.ok) {
        // 401 means not authenticated - user needs to log in
        if (response.status === 401) {
          console.warn('Not authenticated - cannot get WebSocket token');
          return null;
        }
        throw new Error(`Failed to get token: ${response.status}`);
      }

      const data = await response.json();

      if (!data.token) {
        throw new Error('No token in response');
      }

      // Calculate expiry time (with buffer before actual expiry)
      const expiresIn = data.expires_in || 1800; // Default 30 minutes
      const expiresAt = Date.now() + (expiresIn * 1000) - this.EXPIRY_BUFFER_MS;

      // Cache the token
      localStorage.setItem(this.TOKEN_KEY, data.token);
      localStorage.setItem(this.TOKEN_EXPIRY_KEY, expiresAt.toString());

      console.log('WebSocket token fetched, expires in', Math.round((expiresAt - Date.now()) / 1000 / 60), 'minutes');

      return data.token;
    } catch (error) {
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
