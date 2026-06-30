"""Tests for the WebSocket idempotency guard (Layer 1 seatbelt).

Reproduces the 0.5.3a duplicate-restart bug: a reconnect re-sends the same chat
message and the backend must NOT process it twice.
"""
from app.websocket.message_deduplicator import MessageDeduplicator


def test_first_message_is_new():
    d = MessageDeduplicator()
    assert d.register("t1", "abc") is True


def test_duplicate_same_thread_and_id_is_rejected():
    d = MessageDeduplicator()
    assert d.register("t1", "abc") is True
    assert d.register("t1", "abc") is False
    assert d.register("t1", "abc") is False


def test_same_id_different_thread_is_new():
    """Idempotency is scoped per thread; the same id on another thread is new."""
    d = MessageDeduplicator()
    assert d.register("t1", "abc") is True
    assert d.register("t2", "abc") is True


def test_missing_client_message_id_always_new():
    """Legacy clients / Rails gem (no client_message_id) opt out of the guard."""
    d = MessageDeduplicator()
    assert d.register("t1", None) is True
    assert d.register("t1", None) is True
    assert d.register("t1", "") is True


def test_thread_id_coerced_so_int_and_str_match():
    """thread_id arrives as a string over the wire; coercion keeps keys stable."""
    d = MessageDeduplicator()
    assert d.register(123, "abc") is True
    assert d.register("123", "abc") is False


def test_eviction_bounds_memory():
    d = MessageDeduplicator(max_entries=2)
    assert d.register("t", "a") is True
    assert d.register("t", "b") is True
    assert d.register("t", "c") is True  # evicts oldest ("a")
    assert d.register("t", "a") is True  # "a" looks new again after eviction
    assert d.register("t", "c") is False  # "c" still remembered


def test_seen_does_not_record():
    d = MessageDeduplicator()
    assert d.seen("t1", "abc") is False
    assert d.register("t1", "abc") is True
    assert d.seen("t1", "abc") is True
