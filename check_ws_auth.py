#!/usr/bin/env python3
"""Check if WS_AUTH_REQUIRED=true on a live server. No external dependencies.

Usage:
    python check_ws_auth.py <host>
    python check_ws_auth.py mysite.example.com
    python check_ws_auth.py localhost --port 8000 --no-ssl
"""

import socket
import ssl
import json
import base64
import struct
import os
import sys
import argparse


def send_ws_frame(sock, data: str):
    """Send a WebSocket text frame."""
    payload = data.encode('utf-8')
    length = len(payload)

    frame = bytearray([0x81])  # Text frame, FIN bit set

    if length < 126:
        frame.append(0x80 | length)
    elif length < 65536:
        frame.append(0x80 | 126)
        frame.extend(struct.pack('>H', length))
    else:
        frame.append(0x80 | 127)
        frame.extend(struct.pack('>Q', length))

    # Mask key (required for client->server)
    mask_key = os.urandom(4)
    frame.extend(mask_key)

    # Mask the payload
    masked = bytearray(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    frame.extend(masked)

    sock.sendall(frame)


def recv_ws_frame(sock, timeout=5):
    """Receive a WebSocket frame."""
    sock.settimeout(timeout)
    try:
        header = sock.recv(2)
        if len(header) < 2:
            return None

        opcode = header[0] & 0x0F
        length = header[1] & 0x7F

        if length == 126:
            length = struct.unpack('>H', sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack('>Q', sock.recv(8))[0]

        payload = b''
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                break
            payload += chunk

        if opcode == 0x08:  # Close frame
            return None

        return payload.decode('utf-8')
    except socket.timeout:
        return None
    except Exception as e:
        print(f'Recv error: {e}')
        return None


def test_ws_auth(host: str, port: int = 443, use_ssl: bool = True):
    """Test if WebSocket authentication is required on a server."""
    # Create socket
    raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    raw_sock.settimeout(10)

    if use_ssl:
        context = ssl.create_default_context()
        sock = context.wrap_socket(raw_sock, server_hostname=host)
    else:
        sock = raw_sock

    try:
        sock.connect((host, port))
        print(f'TCP connected to {host}:{port}')

        # WebSocket handshake
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f'GET /ws HTTP/1.1\r\n'
            f'Host: {host}\r\n'
            f'Upgrade: websocket\r\n'
            f'Connection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\n'
            f'Sec-WebSocket-Version: 13\r\n'
            f'\r\n'
        )
        sock.sendall(request.encode())

        response = sock.recv(4096).decode()
        if '101' not in response:
            print(f'WebSocket upgrade failed:')
            print(response[:200])
            return

        print('WebSocket connected (101 Switching Protocols)')

        # Test 1: Send ping
        send_ws_frame(sock, json.dumps({'type': 'ping'}))
        print('Sent: ping')

        pong = recv_ws_frame(sock)
        if pong:
            data = json.loads(pong)
            print(f'Received: {data}')
        else:
            print('No pong received')
            return

        # Test 2: Send unauthenticated message
        send_ws_frame(sock, json.dumps({'message': 'test', 'thread_id': 'test-auth-check-123'}))
        print('Sent: unauthenticated chat message')

        response = recv_ws_frame(sock, timeout=10)
        if response:
            data = json.loads(response)
            print(f'Received: {data}')

            if data.get('type') == 'auth_error':
                print()
                print('=' * 50)
                print('WS_AUTH_REQUIRED=true')
                print('WebSocket security is ENABLED')
                print('Unauthenticated messages are REJECTED')
                print('=' * 50)
            else:
                print()
                print('=' * 50)
                print('WS_AUTH_REQUIRED=false')
                print('WebSocket security is DISABLED')
                print('=' * 50)
        else:
            print('Connection closed after unauthenticated message')
            print()
            print('=' * 50)
            print('WS_AUTH_REQUIRED=true (connection closed)')
            print('=' * 50)

    except Exception as e:
        print(f'Error: {type(e).__name__}: {e}')
    finally:
        sock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Check if WS_AUTH_REQUIRED=true on a LlamaBot server'
    )
    parser.add_argument('host', help='Hostname to check (e.g., mysite.example.com)')
    parser.add_argument('--port', type=int, default=443, help='Port (default: 443)')
    parser.add_argument('--no-ssl', action='store_true', help='Disable SSL (for localhost testing)')

    args = parser.parse_args()

    # Default to port 8000 for non-SSL (localhost)
    port = args.port
    if args.no_ssl and args.port == 443:
        port = 8000

    test_ws_auth(args.host, port, use_ssl=not args.no_ssl)
