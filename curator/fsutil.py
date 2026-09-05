"""Atomic file writes (port of ``utils.atomic_write_text`` / ``utils.atomic_json_write``).

Temp file in the target's directory + fsync + ``os.replace`` so a crash never
leaves a half-written sidecar. Shared by every destructive rewrite here.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional, Union


def _existing_mode(path: Path) -> Optional[int]:
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def _atomic_write(path: Path, writer: Callable[[Any], None], *, prefix: str, encoding: str,
                  mode: Optional[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            writer(fh)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Union[str, Path], content: str, *, encoding: str = "utf-8",
                      tmp_prefix: str = ".tmp_", preserve_mode: bool = False,
                      create_mode: Optional[int] = None) -> None:
    """Write *content* atomically. ``preserve_mode`` keeps an existing file's mode;
    ``create_mode`` applies to a NEW file (subject to umask)."""
    path = Path(path)
    mode = None
    if preserve_mode:
        mode = _existing_mode(path)
        if mode is None and create_mode is not None:
            umask = os.umask(0)
            os.umask(umask)
            mode = create_mode & ~umask
    _atomic_write(path, lambda f: f.write(content), prefix=tmp_prefix, encoding=encoding, mode=mode)


def atomic_json_write(path: Union[str, Path], data: Any, *, indent: int = 2, mode: Optional[int] = None,
                      **dump_kwargs: Any) -> None:
    path = Path(path)
    _atomic_write(path, lambda f: json.dump(data, f, indent=indent, ensure_ascii=False, **dump_kwargs),
                  prefix=f".{path.stem}_", encoding="utf-8", mode=mode if mode is not None else _existing_mode(path))
