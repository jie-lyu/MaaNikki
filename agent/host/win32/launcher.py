"""Where the official launcher is installed: registry keys and the launcher's config.ini.

Shared by the start task (first valid installation) and the close task (all
installations, then exactly one containing the game). Each caller keeps its
own error handling; these generators raise whatever the sources raise.
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path

INSTALL_NAMES = (
    "InfinityNikki Launcher",
    "InfinityNikkiBili Launcher",
    "InfinityNikkiGlobal Launcher",
)


def registry_install_dirs():
    """Install folders recorded under HKCU\\Software\\<name>, in INSTALL_NAMES order; missing keys skipped."""
    import winreg
    for name in INSTALL_NAMES:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, rf"Software\{name}") as key:
                folder = Path(winreg.QueryValueEx(key, "")[0])
        except OSError:
            continue
        yield folder


def config_game_dirs():
    """gameDir from %LOCALAPPDATA%\\<name>\\config.ini, in INSTALL_NAMES order; absent files skipped."""
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    for name in INSTALL_NAMES:
        config_path = local_app_data / name / "config.ini"
        if not config_path.is_file():
            continue
        config = configparser.ConfigParser(interpolation=None)
        config.read(config_path, encoding="utf-8-sig")
        directory = config.get("Download", "gameDir", fallback="").strip().strip('"')
        if directory:
            yield Path(directory)
