"""
Tests for WebSocket authentication.

Run with: pytest app/tests/test_websocket_auth.py -v

These tests verify:
1. Token service (JWT creation/validation)
2. WebSocket auth when WS_AUTH_REQUIRED=false (backwards compat)
3. WebSocket auth when WS_AUTH_REQUIRED=true (enforced)
4. Rails token compatibility
"""
import pytest
import json
import os
from unittest.mock import MagicMock, patch


# =============================================================================
# UNIT TESTS - Token Service
# =============================================================================

class TestTokenServiceUnit:
    """Unit tests for token_service.py - no server required."""

    def test_create_token_returns_jwt(self):
        """Test that create_ws_token returns a valid JWT string."""
        from app.services.token_service import create_ws_token

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.role = "engineer"
        mock_user.is_admin = False

        token = create_ws_token(mock_user)

        assert token is not None
        assert isinstance(token, str)
        # JWT tokens have 3 parts separated by dots
        parts = token.split('.')
        assert len(parts) == 3, "JWT should have 3 parts"

    def test_create_token_includes_user_info(self):
        """Test that token payload includes user information."""
        from app.services.token_service import create_ws_token, verify_ws_token

        mock_user = MagicMock()
        mock_user.id = 42
        mock_user.username = "alice"
        mock_user.role = "admin"
        mock_user.is_admin = True

        token = create_ws_token(mock_user)
        payload = verify_ws_token(token)

        assert payload is not None
        assert payload["sub"] == "alice"
        assert payload["user_id"] == 42
        assert payload["role"] == "admin"
        assert payload["is_admin"] is True
        assert payload["type"] == "ws_auth"

    def test_verify_valid_token_succeeds(self):
        """Test that a valid token is accepted."""
        from app.services.token_service import create_ws_token, verify_ws_token

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "bob"
        mock_user.role = "engineer"
        mock_user.is_admin = False

        token = create_ws_token(mock_user)
        payload = verify_ws_token(token)

        assert payload is not None
        assert payload["sub"] == "bob"

    def test_verify_invalid_token_returns_none(self):
        """Test that invalid tokens return None."""
        from app.services.token_service import verify_ws_token

        invalid_tokens = [
            "invalid",
            "not.a.token",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalid.signature",
            "completely-wrong-format",
            "",
        ]

        for token in invalid_tokens:
            result = verify_ws_token(token)
            assert result is None, f"Token '{token}' should be rejected"

    def test_verify_expired_token_returns_none(self):
        """Test that expired tokens are rejected."""
        import jwt
        from datetime import datetime, timedelta, timezone
        from app.services.token_service import verify_ws_token, SECRET_KEY

        # Create an already-expired token
        expired_payload = {
            "sub": "expireduser",
            "user_id": 1,
            "role": "engineer",
            "is_admin": False,
            "type": "ws_auth",
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired_token = jwt.encode(expired_payload, SECRET_KEY, algorithm="HS256")

        result = verify_ws_token(expired_token)
        assert result is None

    def test_verify_wrong_type_token_returns_none(self):
        """Test that tokens with wrong type are rejected."""
        import jwt
        from datetime import datetime, timedelta, timezone
        from app.services.token_service import verify_ws_token, SECRET_KEY

        # Create a token with wrong type
        wrong_type_payload = {
            "sub": "user",
            "user_id": 1,
            "type": "api_auth",  # Wrong type, should be ws_auth
            "iat": datetime.now(timezone.utc),
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        wrong_token = jwt.encode(wrong_type_payload, SECRET_KEY, algorithm="HS256")

        result = verify_ws_token(wrong_token)
        assert result is None

    def test_is_rails_token_detection(self):
        """Test Rails token format detection."""
        from app.services.token_service import is_rails_token

        # Rails tokens have -- separator
        assert is_rails_token("base64data--base64signature") is True
        assert is_rails_token("data--sig") is True
        assert is_rails_token("longer-data-here--signature-here") is True

        # JWT tokens start with eyJ (base64 of {"alg":)
        assert is_rails_token("eyJhbGciOiJIUzI1NiJ9.payload.sig") is False

        # Regular strings without --
        assert is_rails_token("notarailstoken") is False
        assert is_rails_token("single-dash-only") is False
        assert is_rails_token("") is False

    def test_verify_rails_token_trusted(self):
        """Test that Rails tokens are trusted."""
        from app.services.token_service import verify_rails_token

        # Valid Rails-style token
        payload = verify_rails_token("base64data--base64signature")

        assert payload is not None
        assert payload["sub"] == "rails_gem"
        assert payload["type"] == "rails_auth"
        assert payload["source"] == "llama_bot_rails"

    def test_verify_rails_token_rejects_non_rails(self):
        """Test that non-Rails tokens are rejected by verify_rails_token."""
        from app.services.token_service import verify_rails_token

        # JWT-style token (not Rails)
        payload = verify_rails_token("eyJhbGciOiJIUzI1NiJ9.payload.sig")
        assert payload is None

        # Plain string (not Rails)
        payload = verify_rails_token("notarailstoken")
        assert payload is None


# =============================================================================
# INTEGRATION TESTS - WebSocket Handler with WS_AUTH_REQUIRED=false
# =============================================================================

@pytest.mark.integration
class TestWebSocketAuthDisabled:
    """Tests for WebSocket when WS_AUTH_REQUIRED=false (default, backwards compat)."""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set WS_AUTH_REQUIRED=false for these tests."""
        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "false"}):
            # Reload the handler module to pick up the new env
            import importlib
            import app.websocket.web_socket_handler as handler_module
            importlib.reload(handler_module)
            yield
            # Reset after tests
            importlib.reload(handler_module)

    def test_connection_without_auth_succeeds(self):
        """When auth disabled, connections without auth should work."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            # Send ping without auth
            websocket.send_json({"type": "ping"})

            # Should receive pong (connection accepted)
            response = websocket.receive_json()
            assert response["type"] == "pong"

    def test_auth_still_works_when_disabled(self):
        """Even when auth is disabled, valid tokens should still be accepted."""
        from fastapi.testclient import TestClient
        from main import app
        from app.services.token_service import create_ws_token

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.role = "engineer"
        mock_user.is_admin = False
        token = create_ws_token(mock_user)

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "auth", "token": token})

            response = websocket.receive_json()
            assert response["type"] == "auth_success"
            assert response["user"] == "testuser"

    def test_invalid_token_returns_error_but_keeps_connection(self):
        """When auth disabled, invalid token returns error but doesn't close connection."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            # Send invalid token
            websocket.send_json({"type": "auth", "token": "invalid-token"})

            # Should receive auth_error
            response = websocket.receive_json()
            assert response["type"] == "auth_error"

            # Connection should still work - send ping
            websocket.send_json({"type": "ping"})
            response = websocket.receive_json()
            assert response["type"] == "pong"

    def test_message_without_auth_works(self):
        """When auth disabled, regular messages work without auth."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            # Ping should work without auth
            websocket.send_json({"type": "ping"})
            response = websocket.receive_json()
            assert response["type"] == "pong"


# =============================================================================
# INTEGRATION TESTS - WebSocket Handler with WS_AUTH_REQUIRED=true
# =============================================================================

@pytest.mark.integration
class TestWebSocketAuthEnabled:
    """Tests for WebSocket when WS_AUTH_REQUIRED=true (enforced)."""

    @pytest.fixture(autouse=True)
    def setup_env(self):
        """Set WS_AUTH_REQUIRED=true for these tests."""
        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "true"}):
            import importlib
            import app.websocket.web_socket_handler as handler_module
            importlib.reload(handler_module)
            yield
            importlib.reload(handler_module)

    def test_valid_token_succeeds(self):
        """When auth required, valid token should succeed."""
        from fastapi.testclient import TestClient
        from main import app
        from app.services.token_service import create_ws_token

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "authuser"
        mock_user.role = "engineer"
        mock_user.is_admin = False
        token = create_ws_token(mock_user)

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "auth", "token": token})

            response = websocket.receive_json()
            assert response["type"] == "auth_success"
            assert response["user"] == "authuser"

            # After auth, ping should work
            websocket.send_json({"type": "ping"})
            response = websocket.receive_json()
            assert response["type"] == "pong"

    def test_invalid_token_returns_auth_error(self):
        """When auth required, invalid token should return auth_error."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "auth", "token": "bad-token"})
            # Should receive auth_error
            response = websocket.receive_json()
            assert response["type"] == "auth_error"
            assert "content" in response

    def test_ping_works_without_auth(self):
        """Even when auth required, ping should work (keep-alive)."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "ping"})
            response = websocket.receive_json()
            assert response["type"] == "pong"

    def test_regular_message_without_auth_returns_auth_error(self):
        """When auth required, non-ping message without auth should return auth_error."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            # Send regular message without auth
            websocket.send_json({
                "message": "Hello",
                "thread_id": "test-thread"
            })
            # Should receive auth_error
            response = websocket.receive_json()
            assert response["type"] == "auth_error"

    def test_rails_token_succeeds(self):
        """When auth required, Rails tokens should be accepted."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            # Send Rails-style token
            websocket.send_json({
                "type": "auth",
                "token": "rails-data--rails-signature"
            })

            response = websocket.receive_json()
            assert response["type"] == "auth_success"
            assert response["user"] == "rails_gem"

    def test_api_token_in_message_authenticates(self):
        """Rails gem pattern: api_token in message payload should authenticate."""
        from fastapi.testclient import TestClient
        from main import app

        client = TestClient(app)

        with client.websocket_connect("/ws") as websocket:
            # Send message with api_token (Rails gem pattern)
            websocket.send_json({
                "message": "Hello",
                "thread_id": "test",
                "api_token": "rails-data--rails-signature"
            })

            # First response should be auth_success
            response = websocket.receive_json()
            assert response["type"] == "auth_success"


# =============================================================================
# API ENDPOINT TESTS - /api/ws-token
# =============================================================================

@pytest.mark.asyncio
class TestWSTokenEndpoint:
    """Tests for the /api/ws-token endpoint."""

    async def test_ws_token_requires_auth(self, async_client):
        """Test that /api/ws-token requires HTTP Basic auth."""
        response = await async_client.get("/api/ws-token")
        assert response.status_code == 401

    async def test_ws_token_returns_valid_jwt(self, async_client, auth_headers):
        """Test that /api/ws-token returns a valid JWT token."""
        response = await async_client.get("/api/ws-token", headers=auth_headers)

        if response.status_code == 401:
            pytest.skip("No valid auth headers available")

        assert response.status_code == 200
        data = response.json()

        assert "token" in data
        assert "expires_in" in data
        assert isinstance(data["token"], str)
        assert len(data["token"]) > 0
        assert data["expires_in"] == 1800  # 30 minutes

        # Verify it's a valid JWT
        parts = data["token"].split(".")
        assert len(parts) == 3

    async def test_ws_token_is_verifiable(self, async_client, auth_headers):
        """Test that returned token can be verified."""
        from app.services.token_service import verify_ws_token

        response = await async_client.get("/api/ws-token", headers=auth_headers)

        if response.status_code == 401:
            pytest.skip("No valid auth headers available")

        data = response.json()
        payload = verify_ws_token(data["token"])

        assert payload is not None
        assert payload["type"] == "ws_auth"
        assert "sub" in payload
        assert "user_id" in payload
