"""Windows backend. Imported only on Windows, through the accessors in host/__init__.py."""
from __future__ import annotations

from host import Capabilities

CAPABILITIES = Capabilities(
    "win32",
    foreground_input=True,
    background_input=True,
    overlay=True,
    tall_window=True,
    launch_game=True,
    close_game=True,
)
