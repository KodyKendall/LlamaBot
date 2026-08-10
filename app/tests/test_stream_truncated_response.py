"""Mid-stream `RemoteProtocolError: incomplete chunked read` kills the turn.

Production error 92506e269d00 (45 occurrences, 5 instances, v0.6.0c,
rails_beginner_agent on deepseek-v4-flash): the provider accepted the request,
started streaming an SSE body under `Transfer-Encoding: chunked`, then closed
the socket before sending the terminating chunk. httpcore raises
`RemoteProtocolError`, httpx re-raises it as `httpx.RemoteProtocolError`, and —
because the openai SDK does NOT wrap errors raised while *reading the body*
(only ones raised while making the request) — it propagates raw out of
`llm.invoke()`, up through the LangGraph node, and out of `app.astream()` in
request_handler.handle_request. The turn dies.

These tests reproduce the failure against the REAL stack — a real socket
serving a real truncated chunked SSE body, a real httpx/openai/langchain_openai
client — rather than by raising the exception by hand, because the thing worth
pinning is that this failure mode reaches our code as
`httpx.RemoteProtocolError` at all. If the SDK ever starts wrapping it, the
transient classifier stops matching and every retry rung silently stops firing.

Covers the raw-StateGraph nodes that call the model directly and therefore never
run `DynamicModelMiddleware.wrap_model_call` — the only other place the retry
ladder lives.
"""
import json
import socket
import threading

import httpx
import pytest

from app.agents.leonardo.resilience import is_transient_error


# =============================================================================
# A real HTTP server that truncates a chunked SSE body mid-stream
# =============================================================================

def _sse_chunk(text: str, finish: str | None = None) -> bytes:
    payload = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "deepseek-v4-flash",
        "choices": [{
            "index": 0,
            "delta": {"role": "assistant", "content": text},
            "finish_reason": finish,
        }],
    }
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _chunked(body: bytes) -> bytes:
    """Frame one HTTP/1.1 chunked-transfer chunk (without the terminator)."""
    return f"{len(body):x}\r\n".encode() + body + b"\r\n"


_HEADERS = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream\r\n"
    b"Transfer-Encoding: chunked\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


class TruncatingSSEServer:
    """Serves `fail_first` truncated streams, then complete ones.

    A truncated stream = headers + one real SSE chunk + socket close, with no
    terminating `0\\r\\n\\r\\n`. That is exactly the wire behavior behind
    "peer closed connection without sending complete message body
    (incomplete chunked read)".
    """

    def __init__(self, fail_first: int = 1):
        self.fail_first = fail_first
        self.requests = 0
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        try:
            # Drain the request (headers + Content-Length body) so the client
            # sees a well-formed exchange up to the point we break it.
            data = b""
            while b"\r\n\r\n" not in data:
                part = conn.recv(65536)
                if not part:
                    return
                data += part
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1])
            while len(rest) < length:
                part = conn.recv(65536)
                if not part:
                    break
                rest += part

            self.requests += 1
            truncate = self.requests <= self.fail_first

            conn.sendall(_HEADERS)
            conn.sendall(_chunked(_sse_chunk("Hel")))
            if truncate:
                # Close mid-body: no final SSE event, no `0\r\n\r\n` terminator.
                conn.shutdown(socket.SHUT_RDWR)
                return
            conn.sendall(_chunked(_sse_chunk("lo", finish="stop")))
            conn.sendall(_chunked(b"data: [DONE]\n\n"))
            conn.sendall(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


def _client(base_url: str):
    """A real streaming DeepSeek client pointed at the fake server."""
    from app.agents.leonardo.llm_factory import ChatDeepSeekWithReasoning
    return ChatDeepSeekWithReasoning(
        model="deepseek-v4-flash",
        api_key="test-key",
        api_base=base_url,
        streaming=True,      # the production path streams — see the traceback
        max_retries=0,       # provider-SDK retries are off in get_llm()
        timeout=10,
    )


# =============================================================================
# 1. The bare reproduction — this is the production exception, on real sockets
# =============================================================================

def test_truncated_chunked_stream_raises_remote_protocol_error():
    """A stream cut off mid-body surfaces as httpx.RemoteProtocolError."""
    with TruncatingSSEServer(fail_first=1) as server:
        with pytest.raises(httpx.RemoteProtocolError) as excinfo:
            _client(server.base_url).invoke([{"role": "user", "content": "hi"}])

    assert "incomplete chunked read" in str(excinfo.value)


def test_that_exception_is_classified_transient():
    """If it isn't transient, every retry rung silently no-ops for this error."""
    with TruncatingSSEServer(fail_first=1) as server:
        with pytest.raises(httpx.RemoteProtocolError) as excinfo:
            _client(server.base_url).invoke([{"role": "user", "content": "hi"}])

    assert is_transient_error(excinfo.value) is True


# =============================================================================
# 2. The nodes that call the model directly (no DynamicModelMiddleware)
# =============================================================================

def _state(messages):
    return {"messages": messages, "llm_model": "deepseek-v4-flash"}


@pytest.mark.parametrize("module_path,node_name", [
    ("app.agents.leonardo.rails_beginner_agent.nodes", "leonardo_beginner"),
    ("app.agents.leonardo.rails_plain_chat_mode.nodes", "plain_chat"),
    ("app.agents.leonardo.rails_ai_builder_agent.nodes", "leonardo_ai_builder"),
])
def test_raw_node_survives_truncated_stream(module_path, node_name, monkeypatch):
    """A single truncated stream must not kill the turn.

    Each of these is a raw StateGraph node: it calls `.invoke()` on the model
    itself, so `DynamicModelMiddleware.wrap_model_call` never runs and the retry
    has to be at the call site.
    """
    import importlib
    from langchain_core.messages import HumanMessage

    mod = importlib.import_module(module_path)
    node = getattr(mod, node_name)

    with TruncatingSSEServer(fail_first=1) as server:
        monkeypatch.setattr(mod, "get_llm", lambda *_a, **_k: _client(server.base_url))
        # Don't pay the real backoff sleep.
        import app.agents.leonardo.resilience as res
        monkeypatch.setattr(res.time, "sleep", lambda *_: None)

        result = node(_state([HumanMessage(content="hi")]))

    assert server.requests == 2, "expected one failure then one retry"
    assert result["messages"][0].content == "Hello"
