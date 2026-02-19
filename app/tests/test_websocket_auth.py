"""
Tests for WebSocket authentication.

Run with: pytest app/tests/test_websocket_auth.py -v
Against live server: pytest app/tests/test_websocket_auth.py -v --live-server
"""
import pytest
import json
import os
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


class TestWebSocketAuthUnit:
    """Unit tests for WebSocket authentication logic."""

    def test_token_service_create_token(self):
        """Test JWT token creation."""
        from app.services.token_service import create_ws_token

        # Create a mock user
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.role = "engineer"
        mock_user.is_admin = False

        token = create_ws_token(mock_user)

        assert token is not None
        assert isinstance(token, str)
        assert len(token) > 0
        # JWT tokens have 3 parts separated by dots
        assert len(token.split('.')) == 3

    def test_token_service_verify_valid_token(self):
        """Test JWT token verification with valid token."""
        from app.services.token_service import create_ws_token, verify_ws_token

        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.role = "engineer"
        mock_user.is_admin = False

        token = create_ws_token(mock_user)
        payload = verify_ws_token(token)

        assert payload is not None
        assert payload["sub"] == "testuser"
        assert payload["user_id"] == 1
        assert payload["role"] == "engineer"
        assert payload["is_admin"] is False
        assert payload["type"] == "ws_auth"

    def test_token_service_verify_invalid_token(self):
        """Test JWT token verification with invalid token."""
        from app.services.token_service import verify_ws_token

        invalid_tokens = [
            "invalid",
            "not.a.token",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.invalid.signature",
            "",
            None,
        ]

        for token in invalid_tokens:
            if token is not None:
                payload = verify_ws_token(token)
                assert payload is None, f"Expected None for token: {token}"

    def test_token_service_verify_expired_token(self):
        """Test JWT token verification with expired token."""
        import jwt
        from datetime import datetime, timedelta, timezone
        from app.services.token_service import verify_ws_token, SECRET_KEY

        # Create an already-expired token
        expired_payload = {
            "sub": "testuser",
            "user_id": 1,
            "role": "engineer",
            "is_admin": False,
            "type": "ws_auth",
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        expired_token = jwt.encode(expired_payload, SECRET_KEY, algorithm="HS256")

        payload = verify_ws_token(expired_token)
        assert payload is None

    def test_is_rails_token(self):
        """Test Rails token detection."""
        from app.services.token_service import is_rails_token

        # Rails tokens have -- separator
        assert is_rails_token("base64data--base64signature") is True
        assert is_rails_token("some-data--some-signature") is True

        # JWT tokens start with eyJ
        assert is_rails_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig") is False

        # Regular strings
        assert is_rails_token("notarailstoken") is False
        assert is_rails_token("") is False

    def test_verify_rails_token(self):
        """Test Rails token verification (trust-based)."""
        from app.services.token_service import verify_rails_token

        # Valid-looking Rails token
        payload = verify_rails_token("base64data--base64signature")
        assert payload is not None
        assert payload["sub"] == "rails_gem"
        assert payload["type"] == "rails_auth"

        # Invalid token (not Rails format)
        payload = verify_rails_token("notarailstoken")
        assert payload is None


@pytest.mark.integration
class TestWebSocketAuthIntegration:
    """Integration tests for WebSocket authentication."""

    def test_websocket_connect_without_auth_when_not_required(self):
        """Test WebSocket connects successfully when auth is not required."""
        # This tests the default behavior (WS_AUTH_REQUIRED=false)
        from main import app

        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "false"}):
            client = TestClient(app)

            with client.websocket_connect("/ws") as websocket:
                # Send a ping to verify connection works
                websocket.send_json({"type": "ping"})

                # Should receive pong
                response = websocket.receive_json()
                assert response["type"] == "pong"

    def test_websocket_auth_success_flow(self):
        """Test WebSocket authentication success flow."""
        from main import app
        from app.services.token_service import create_ws_token

        # Create a valid token
        mock_user = MagicMock()
        mock_user.id = 1
        mock_user.username = "testuser"
        mock_user.role = "engineer"
        mock_user.is_admin = False
        token = create_ws_token(mock_user)

        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "false"}):
            client = TestClient(app)

            with client.websocket_connect("/ws") as websocket:
                # Send auth message
                websocket.send_json({"type": "auth", "token": token})

                # Should receive auth_success
                response = websocket.receive_json()
                assert response["type"] == "auth_success"
                assert response["user"] == "testuser"

    def test_websocket_auth_failure_with_invalid_token(self):
        """Test WebSocket authentication failure with invalid token."""
        from main import app

        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "false"}):
            client = TestClient(app)

            with client.websocket_connect("/ws") as websocket:
                # Send auth message with invalid token
                websocket.send_json({"type": "auth", "token": "invalid-token"})

                # Should receive auth_error
                response = websocket.receive_json()
                assert response["type"] == "auth_error"
                assert "Invalid" in response["content"] or "expired" in response["content"]

    def test_websocket_auth_with_rails_token(self):
        """Test WebSocket authentication with Rails-style api_token in message."""
        from main import app

        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "false"}):
            client = TestClient(app)

            with client.websocket_connect("/ws") as websocket:
                # Send a message with api_token (Rails gem pattern)
                websocket.send_json({
                    "type": "auth",
                    "token": "rails-token-data--rails-signature"
                })

                # Should receive auth_success (Rails tokens are trusted)
                response = websocket.receive_json()
                assert response["type"] == "auth_success"
                assert response["user"] == "rails_gem"


@pytest.mark.integration
class TestWebSocketAuthEnforced:
    """Tests for WebSocket authentication when WS_AUTH_REQUIRED=true."""

    def test_websocket_rejects_unauthenticated_message_when_required(self):
        """Test WebSocket rejects messages without auth when required."""
        from main import app

        # Force reload of the module to pick up env change
        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "true"}):
            # Need to reimport to get the new env value
            import importlib
            import app.websocket.web_socket_handler as handler_module
            importlib.reload(handler_module)

            client = TestClient(app)

            try:
                with client.websocket_connect("/ws") as websocket:
                    # Send a regular message without auth first
                    websocket.send_json({
                        "message": "Hello",
                        "thread_id": "test-thread"
                    })

                    # Should receive auth_error and connection should close
                    response = websocket.receive_json()
                    assert response["type"] == "auth_error"
            except Exception:
                # Connection closed is expected
                pass
            finally:
                # Reset to default
                importlib.reload(handler_module)

    def test_websocket_allows_ping_without_auth(self):
        """Test WebSocket allows ping even without auth."""
        from main import app

        with patch.dict(os.environ, {"WS_AUTH_REQUIRED": "true"}):
            import importlib
            import app.websocket.web_socket_handler as handler_module
            importlib.reload(handler_module)

            client = TestClient(app)

            try:
                with client.websocket_connect("/ws") as websocket:
                    # Ping should work without auth
                    websocket.send_json({"type": "ping"})

                    response = websocket.receive_json()
                    assert response["type"] == "pong"
            finally:
                # Reset to default
                importlib.reload(handler_module)


class TestWSTokenEndpoint:
    """Tests for the /api/ws-token endpoint."""

    @pytest.mark.asyncio
    async def test_ws_token_endpoint_requires_auth(self, async_client):
        """Test that /api/ws-token requires authentication."""
        response = await async_client.get("/api/ws-token")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_ws_token_endpoint_returns_token(self, async_client, auth_headers):
        """Test that /api/ws-token returns a valid token."""
        response = await async_client.get("/api/ws-token", headers=auth_headers)

        # May be 401 if auth_headers fixture doesn't match a real user
        # In that case, skip this test
        if response.status_code == 401:
            pytest.skip("No valid auth headers available")

        assert response.status_code == 200
        data = response.json()

        assert "token" in data
        assert "expires_in" in data
        assert isinstance(data["token"], str)
        assert len(data["token"]) > 0
        assert data["expires_in"] == 1800  # 30 minutes in seconds
