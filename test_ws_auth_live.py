#!/usr/bin/env python3
"""
Live WebSocket authentication tests.

Run against a live server at localhost:8000.

Usage:
    python test_ws_auth_live.py

Tests:
1. Connection without auth - verifies WebSocket accepts connections
2. Auth with valid JWT token - verifies JWT auth flow works
3. Auth with invalid token - verifies invalid tokens are rejected
4. Auth with Rails token - verifies Rails gem tokens are trusted

Note: Test 2 requires valid HTTP Basic credentials to fetch a JWT token.
      Edit the username/password variables if needed.
"""
import asyncio
import json
import sys

# Use websockets library if available, otherwise fallback
try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    print("Note: websockets library not installed, using basic socket test")

import socket
import base64
import hashlib


def create_websocket_handshake(host: str, port: int, path: str) -> tuple[socket.socket, bool]:
    """Create a WebSocket connection using raw sockets."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(5)

    try:
        sock.connect((host, port))

        # Generate WebSocket key
        key = base64.b64encode(b'test-websocket-key').decode()

        # Send HTTP upgrade request
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            f"\r\n"
        )
        sock.sendall(request.encode())

        # Receive response
        response = sock.recv(4096).decode()

        if "101 Switching Protocols" in response:
            return sock, True
        else:
            sock.close()
            return None, False
    except Exception as e:
        print(f"Connection error: {e}")
        sock.close()
        return None, False


def send_ws_frame(sock: socket.socket, data: str) -> None:
    """Send a WebSocket text frame."""
    payload = data.encode()
    length = len(payload)

    # Build frame header
    frame = bytearray()
    frame.append(0x81)  # Text frame, FIN bit set

    # Mask bit must be set for client->server
    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(length.to_bytes(2, 'big'))
    else:
        frame.append(0x80 | 127)
        frame.extend(length.to_bytes(8, 'big'))

    # Add mask key (required for client)
    mask_key = b'\x00\x00\x00\x00'  # Simple mask
    frame.extend(mask_key)

    # Mask the payload
    masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    frame.extend(masked)

    sock.sendall(frame)


def recv_ws_frame(sock: socket.socket) -> str:
    """Receive a WebSocket text frame."""
    try:
        # Read frame header
        header = sock.recv(2)
        if len(header) < 2:
            return None

        opcode = header[0] & 0x0F
        is_masked = header[1] & 0x80
        length = header[1] & 0x7F

        if length == 126:
            length = int.from_bytes(sock.recv(2), 'big')
        elif length == 127:
            length = int.from_bytes(sock.recv(8), 'big')

        # Server frames are not masked
        payload = sock.recv(length)

        if opcode == 0x08:  # Close frame
            return None

        return payload.decode()
    except Exception as e:
        print(f"Receive error: {e}")
        return None


def test_connection_without_auth():
    """Test 1: Verify WebSocket connects (auth not required by default)."""
    print("\n=== Test 1: Connection without auth ===")

    sock, connected = create_websocket_handshake("localhost", 8000, "/ws")
    if not connected:
        print("FAIL: Could not establish WebSocket connection")
        return False

    print("Connected to WebSocket (101 Switching Protocols)")

    # Send ping
    send_ws_frame(sock, json.dumps({"type": "ping"}))
    print("Sent: ping")

    # Receive pong
    response = recv_ws_frame(sock)
    if response:
        data = json.loads(response)
        if data.get("type") == "pong":
            print(f"Received: pong")
            print("PASS: Connection works without auth (WS_AUTH_REQUIRED=false)")
            sock.close()
            return True
        else:
            print(f"Received unexpected: {data}")

    sock.close()
    print("FAIL: Did not receive expected pong")
    return False


def test_auth_with_valid_token():
    """Test 2: Authenticate with a valid JWT token."""
    print("\n=== Test 2: Auth with valid JWT token ===")

    # First, we need to get a token from the API
    # This requires HTTP Basic auth
    import urllib.request
    import base64

    # Note: This will fail without valid credentials
    # For manual testing, you can hardcode credentials here
    username = "admin"
    password = "admin"

    try:
        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        req = urllib.request.Request(
            "http://localhost:8000/api/ws-token",
            headers={"Authorization": f"Basic {credentials}"}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            token_data = json.loads(response.read().decode())
            token = token_data["token"]
            print(f"Got token: {token[:50]}...")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("SKIP: Cannot get token - HTTP Basic auth failed")
            print("       (Set valid username/password in test script)")
            return None
        raise
    except Exception as e:
        print(f"SKIP: Cannot get token - {e}")
        return None

    # Connect and authenticate
    sock, connected = create_websocket_handshake("localhost", 8000, "/ws")
    if not connected:
        print("FAIL: Could not establish WebSocket connection")
        return False

    # Send auth message
    send_ws_frame(sock, json.dumps({"type": "auth", "token": token}))
    print("Sent: auth message with token")

    # Receive auth response
    response = recv_ws_frame(sock)
    if response:
        data = json.loads(response)
        if data.get("type") == "auth_success":
            print(f"Received: auth_success (user: {data.get('user')})")
            print("PASS: Authentication succeeded")
            sock.close()
            return True
        else:
            print(f"Received: {data}")
            sock.close()
            return False

    sock.close()
    print("FAIL: No response received")
    return False


def test_auth_with_invalid_token():
    """Test 3: Authenticate with an invalid token."""
    print("\n=== Test 3: Auth with invalid token ===")

    sock, connected = create_websocket_handshake("localhost", 8000, "/ws")
    if not connected:
        print("FAIL: Could not establish WebSocket connection")
        return False

    # Send auth message with invalid token
    send_ws_frame(sock, json.dumps({"type": "auth", "token": "invalid-token-12345"}))
    print("Sent: auth message with invalid token")

    # Receive auth response
    response = recv_ws_frame(sock)
    if response:
        data = json.loads(response)
        if data.get("type") == "auth_error":
            print(f"Received: auth_error ({data.get('content')})")
            print("PASS: Invalid token correctly rejected")
            sock.close()
            return True
        else:
            print(f"Received unexpected: {data}")

    sock.close()
    print("FAIL: Did not receive expected auth_error")
    return False


def test_auth_with_rails_token():
    """Test 4: Authenticate with a Rails-style token."""
    print("\n=== Test 4: Auth with Rails token ===")

    sock, connected = create_websocket_handshake("localhost", 8000, "/ws")
    if not connected:
        print("FAIL: Could not establish WebSocket connection")
        return False

    # Send auth message with Rails-style token (has -- separator)
    rails_token = "YmFzZTY0ZGF0YQ==--YmFzZTY0c2lnbmF0dXJl"
    send_ws_frame(sock, json.dumps({"type": "auth", "token": rails_token}))
    print(f"Sent: auth message with Rails token")

    # Receive auth response
    response = recv_ws_frame(sock)
    if response:
        data = json.loads(response)
        if data.get("type") == "auth_success":
            print(f"Received: auth_success (user: {data.get('user')})")
            print("PASS: Rails token accepted (trusted internal service)")
            sock.close()
            return True
        else:
            print(f"Received: {data}")

    sock.close()
    print("FAIL: Rails token not accepted")
    return False


def main():
    print("=" * 60)
    print("WebSocket Authentication Live Tests")
    print("Server: ws://localhost:8000/ws")
    print("=" * 60)

    results = []

    # Test 1: Connection without auth
    results.append(("Connection without auth", test_connection_without_auth()))

    # Test 2: Auth with valid token
    results.append(("Auth with valid token", test_auth_with_valid_token()))

    # Test 3: Auth with invalid token
    results.append(("Auth with invalid token", test_auth_with_invalid_token()))

    # Test 4: Auth with Rails token
    results.append(("Auth with Rails token", test_auth_with_rails_token()))

    # Summary
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)

    passed = 0
    failed = 0
    skipped = 0

    for name, result in results:
        if result is True:
            status = "PASS"
            passed += 1
        elif result is False:
            status = "FAIL"
            failed += 1
        else:
            status = "SKIP"
            skipped += 1
        print(f"  {status}: {name}")

    print()
    print(f"Passed: {passed}, Failed: {failed}, Skipped: {skipped}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
