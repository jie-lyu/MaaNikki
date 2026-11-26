"""Detailed task messages persisted independently of the UI focus channel."""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import os
from threading import Lock

from app_paths import DATA_ROOT


_ACTION = ContextVar("maanikki_message_action", default=None)
_LOCK = Lock()
# The client exports and clears .log files recursively under debug/.
_DIRECTORY = DATA_ROOT / "debug/messages"


@contextmanager
def message_scope(action):
    token = _ACTION.set(action)
    try:
        yield
    finally:
        _ACTION.reset(token)


def write_message(text, display, *, report=None):
    """Append before UI delivery, including file-only and stopped-task messages.

    ``report``: a result written before its display (Android: the text is displayed only after the stop is asked
    again), kept as its own field so tools can show it; None (every other caller) adds nothing to the record.

    Return False on an IO failure so the caller can keep detail visible rather
    than silently discarding the only human-readable copy.
    """
    now = datetime.now().astimezone()
    record = {"timestamp": now.isoformat(timespec="milliseconds"), "pid": os.getpid(),
              "action": _ACTION.get(), "message": text, "display": display}
    if report is not None:
        record["report"] = report
    try:
        with _LOCK:
            _DIRECTORY.mkdir(parents=True, exist_ok=True)
            path = _DIRECTORY / f"{now:%Y%m%d}-{os.getpid()}.log"
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False
