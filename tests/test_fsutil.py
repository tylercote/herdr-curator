"""curator.fsutil — atomic writes (port of the two ``utils`` helpers Hermes uses)."""

from __future__ import annotations

import json
import os
import stat


def test_atomic_write_text_leaves_no_temp_files(tmp_path):
    from curator.fsutil import atomic_write_text
    target = tmp_path / "f.txt"
    atomic_write_text(target, "hello", tmp_prefix=".usage_")
    assert target.read_text(encoding="utf-8") == "hello"
    assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]


def test_atomic_write_text_preserves_mode_when_asked(tmp_path):
    from curator.fsutil import atomic_write_text
    target = tmp_path / "f.txt"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o600)
    atomic_write_text(target, "y", preserve_mode=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_atomic_write_text_create_mode_for_new_files(tmp_path):
    from curator.fsutil import atomic_write_text
    target = tmp_path / "new.md"
    atomic_write_text(target, "y", preserve_mode=True, create_mode=0o644)
    assert stat.S_IMODE(target.stat().st_mode) == 0o644 & ~_umask()


def _umask() -> int:
    cur = os.umask(0)
    os.umask(cur)
    return cur


def test_atomic_json_write_roundtrip_sorted(tmp_path):
    from curator.fsutil import atomic_json_write
    target = tmp_path / "s.json"
    atomic_json_write(target, {"b": 1, "a": [1, 2]}, indent=2, sort_keys=True)
    text = target.read_text(encoding="utf-8")
    assert json.loads(text) == {"a": [1, 2], "b": 1}
    assert text.index('"a"') < text.index('"b"')
    assert not [p for p in tmp_path.iterdir() if p.name != "s.json"]


def test_atomic_write_creates_parent_dirs(tmp_path):
    from curator.fsutil import atomic_write_text
    target = tmp_path / "deep" / "er" / "f.txt"
    atomic_write_text(target, "z")
    assert target.read_text(encoding="utf-8") == "z"
