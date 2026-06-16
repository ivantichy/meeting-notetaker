"""PyInstaller entry point for Meeting Notetaker.

The application stores its working data (config.json, notetaker.log, notes/,
and the downloaded Whisper models/) relative to the *current working
directory*. When frozen by PyInstaller we point that working directory at the
folder next to the executable (the per-user install dir under
%LOCALAPPDATA%\\Programs\\MeetingNotetaker), which is always user-writable.

app.main.main() additionally calls os.chdir(Path(__file__).parents[1]) to
locate its own root. Inside a frozen bundle that path is the bundled
``_internal`` folder, which is the wrong place for user data. We cannot edit
app/main.py, so we neutralise that single chdir by pinning the working
directory: os.chdir is wrapped so it only honours the very first call made by
this entry script and turns the app's later chdir into a no-op.
"""
from __future__ import annotations

import os
import sys


def _app_base_dir() -> str:
    """Directory that should hold config.json / notes / models / logs."""
    if getattr(sys, "frozen", False):
        # Folder containing MeetingNotetaker.exe (onedir layout).
        return os.path.dirname(sys.executable)
    # Running from source: project root (two levels up from this file).
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    base = _app_base_dir()
    os.makedirs(base, exist_ok=True)
    os.chdir(base)

    # Pin the working directory: ignore any further chdir (e.g. the one inside
    # app.main.main that would otherwise jump into the bundled _internal dir).
    _real_chdir = os.chdir

    def _locked_chdir(_path):  # noqa: ANN001
        return None

    os.chdir = _locked_chdir  # type: ignore[assignment]
    try:
        from app.main import main as app_main

        return app_main()
    finally:
        os.chdir = _real_chdir  # type: ignore[assignment]


if __name__ == "__main__":
    sys.exit(main())
