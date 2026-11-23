"""Shared atomic JSON snapshots; task owners decide how write failures surface."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time


def write_json(path, data):
    """Publish a complete UTF-8 snapshot, leaving the old file intact on failure."""
    path = Path(path)
    content = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, filename = tempfile.mkstemp(prefix=path.name+".", suffix=".tmp", dir=path.parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/scanners may briefly deny deletion of either file.
        # Retry only the atomic publish, keeping the fsynced snapshot and old
        # destination intact. Persistent access failures still reach callers
        # after at most 0.75 seconds of backoff; successful writes never wait.
        for attempt in range(5):
            try:
                temporary.replace(path)
                break
            except PermissionError as error:
                if (os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33}
                        or attempt == 4):
                    raise
                time.sleep(.05 * (2 ** attempt))
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_file_lock(path):
    """Hold an exclusive lock on path across processes (byte 0 via msvcrt on Windows, flock elsewhere).

    The client's Rust account writer locks the same account lock file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
