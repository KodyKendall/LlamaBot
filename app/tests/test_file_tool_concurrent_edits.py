"""Two edits to one file in one message must both survive.

`leo-fotesu`, 2026-08-12: the agent issued two `edit_file` calls against
`app/views/sources/_source.html.erb` in a single assistant message. Both
returned "Successfully replaced string"; the next read showed only one of them.

Tool calls in one message run concurrently, so both read the same base content
and the last write wins — silently, because each call reports success against
the content it read. That is data loss the agent cannot see.
"""

import threading
from pathlib import Path

import pytest

from app.agents.leonardo.rails_agent import tools


class _Runtime:
    def __init__(self, tool_call_id):
        self.tool_call_id = tool_call_id


@pytest.fixture
def rails_root(tmp_path, monkeypatch):
    root = tmp_path / "rails"
    (root / "app" / "views").mkdir(parents=True)
    monkeypatch.setattr(tools, "RAILS_ROOT", root)
    monkeypatch.setattr(tools, "PROJECT_ROOT", tmp_path)
    return root


def _run_concurrently(calls, read_barrier_timeout=1.0):
    """Run `calls` in threads, forcing both to read before either writes.

    Without serialization both threads see the same base content — the exact
    interleaving from the incident. With it, the second thread cannot reach its
    read until the first has written, so its wait simply times out and it reads
    the up-to-date file.
    """
    read_gate = threading.Barrier(len(calls), timeout=read_barrier_timeout)
    original_read_text = Path.read_text

    def read_then_wait(self, *args, **kwargs):
        content = original_read_text(self, *args, **kwargs)
        try:
            read_gate.wait()
        except threading.BrokenBarrierError:
            pass  # serialized — the other call is behind the lock, as intended
        return content

    Path.read_text = read_then_wait
    try:
        threads = [threading.Thread(target=call) for call in calls]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    finally:
        Path.read_text = original_read_text


def test_two_edits_to_the_same_file_both_land(rails_root):
    target = rails_root / "app" / "views" / "_source.html.erb"
    target.write_text("<h1>ALPHA</h1>\n<p>BRAVO</p>\n")

    results = {}

    def edit(name, old, new):
        def run():
            results[name] = tools.edit_file.func(
                "app/views/_source.html.erb", old, new, runtime=_Runtime(name)
            )
        return run

    _run_concurrently([
        edit("first", "ALPHA", "ALPHA-EDITED"),
        edit("second", "BRAVO", "BRAVO-EDITED"),
    ])

    content = target.read_text()
    assert "ALPHA-EDITED" in content, f"first edit was lost: {content!r}"
    assert "BRAVO-EDITED" in content, f"second edit was lost: {content!r}"


def test_a_write_and_an_edit_to_the_same_file_do_not_interleave(rails_root):
    """write_file overwrites wholesale, so it must take the same lock."""
    target = rails_root / "app" / "views" / "_source.html.erb"
    target.write_text("<h1>ALPHA</h1>\n")

    def do_edit():
        tools.edit_file.func(
            "app/views/_source.html.erb", "ALPHA", "ALPHA-EDITED",
            runtime=_Runtime("edit"),
        )

    def do_write():
        tools.write_file.func(
            "app/views/_source.html.erb", "<h1>ALPHA</h1>\n<p>ADDED</p>\n",
            runtime=_Runtime("write"),
        )

    _run_concurrently([do_edit, do_write])

    content = target.read_text()
    # Whichever ran second wins the file, but the loser must not have written a
    # version built from content it read before the winner's write.
    assert content in (
        "<h1>ALPHA</h1>\n<p>ADDED</p>\n",     # write ran last
        "<h1>ALPHA-EDITED</h1>\n<p>ADDED</p>\n",  # edit ran last, on fresh content
    ), content
