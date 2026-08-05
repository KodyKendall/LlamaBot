"""Regression tests for `delete_memory_file` (fleet fingerprint f8d5a76106fb).

The LLM called the `delete_memory` tool with a blank `filename`. `Path(MEMORY_DIR) / ""`
collapses to MEMORY_DIR itself, `.exists()` passed, and `unlink()` raised
IsADirectoryError — which LangGraph's default tool-error handler re-raises, killing the
whole chat turn. A tool fed bad LLM input must return an error result, never raise.

The same call path had no path confinement, so `"../escape.txt"` would unlink files
outside the memory directory.
"""

import importlib

import pytest


@pytest.fixture
def memory_mod(tmp_path, monkeypatch):
    """Point the memory module at a throwaway directory."""
    import app.agents.leonardo.memory as memory

    importlib.reload(memory)
    mem_dir = tmp_path / ".leonardo" / "memory"
    mem_dir.mkdir(parents=True)
    monkeypatch.setattr(memory, "MEMORY_DIR", str(mem_dir))
    monkeypatch.setattr(memory, "MEMORY_MD_PATH", str(tmp_path / ".leonardo" / "MEMORY.md"))
    memory._test_dir = tmp_path  # convenience handle for the tests
    return memory


def _write_memory(memory_mod, filename: str, name: str = "sample") -> None:
    from pathlib import Path

    (Path(memory_mod.MEMORY_DIR) / filename).write_text(
        f"---\nname: {name}\ndescription: a {name}\ntype: project\n---\n\nbody\n",
        encoding="utf-8",
    )


def test_blank_filename_returns_false_instead_of_raising(memory_mod):
    """The exact production crash: blank filename must not raise IsADirectoryError."""
    assert memory_mod.delete_memory_file("") is False
    assert memory_mod.delete_memory_file("   ") is False


def test_none_filename_returns_false(memory_mod):
    assert memory_mod.delete_memory_file(None) is False


def test_directory_name_returns_false_and_survives(memory_mod):
    """A subdirectory inside the memory dir must never be unlinked."""
    from pathlib import Path

    subdir = Path(memory_mod.MEMORY_DIR) / "archive"
    subdir.mkdir()

    assert memory_mod.delete_memory_file("archive") is False
    assert subdir.is_dir()


def test_path_traversal_is_refused_and_target_survives(memory_mod):
    """`../escape.md` resolves outside MEMORY_DIR and must be rejected."""
    from pathlib import Path

    outside = Path(memory_mod.MEMORY_DIR).parent / "escape.md"
    outside.write_text("do not delete me", encoding="utf-8")

    assert memory_mod.delete_memory_file("../escape.md") is False
    assert outside.exists(), "traversal deleted a file outside the memory directory"


def test_absolute_path_is_refused(memory_mod, tmp_path):
    outside = tmp_path / "absolute.md"
    outside.write_text("do not delete me", encoding="utf-8")

    assert memory_mod.delete_memory_file(str(outside)) is False
    assert outside.exists()


def test_missing_file_returns_false(memory_mod):
    assert memory_mod.delete_memory_file("nope.md") is False


def test_real_memory_still_deletes_and_rebuilds_index(memory_mod):
    """The happy path must keep working, including the index rebuild."""
    from pathlib import Path

    _write_memory(memory_mod, "sample.md", name="sample")
    _write_memory(memory_mod, "keeper.md", name="keeper")
    target = Path(memory_mod.MEMORY_DIR) / "sample.md"
    assert target.exists()

    assert memory_mod.delete_memory_file("sample.md") is True
    assert not target.exists()

    index = Path(memory_mod.MEMORY_MD_PATH)
    assert index.exists(), "index was not rebuilt"
    index_text = index.read_text(encoding="utf-8")
    assert "keeper" in index_text
    assert "sample.md" not in index_text, "index still references the deleted memory"
