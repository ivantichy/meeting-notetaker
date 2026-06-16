"""Plánovač: čistá funkce rozhodující o auto start/stop nahrávání."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.config import AppConfig
from app.models import Meeting, Platform, RecorderState

_AUTO_PLATFORMS = (Platform.MEET, Platform.TEAMS)


def pick_action(
    now: datetime,
    meetings: "list[Meeting]",
    state: RecorderState,
    current: "Meeting | None",
    cfg: AppConfig,
) -> "tuple[str, Meeting | None]":
    """Returns one of: ("none",None) ("arm",m) ("start",m) ("stop",current).

    - arm: state==IDLE and a Meet/Teams meeting starts within arm_window_s
    - start: state in (IDLE, ARMED) and now >= m.start and now <= m.end+stop_grace
    - stop: state==RECORDING and now > current.end + stop_grace_s
    Overlapping meetings: earliest start wins; never auto-stop early for the next one.
    """
    grace = timedelta(seconds=cfg.stop_grace_s)
    arm_window = timedelta(seconds=cfg.arm_window_s)

    if state == RecorderState.RECORDING:
        # Nikdy nezastavovat dřív kvůli další schůzce — jen po uplynutí grace.
        if current is not None and now > current.end + grace:
            return ("stop", current)
        return ("none", None)

    if state in (RecorderState.IDLE, RecorderState.ARMED):
        # Probíhající schůzky (včetně restartu aplikace uprostřed schůzky).
        running = [
            m
            for m in meetings
            if m.platform in _AUTO_PLATFORMS and m.start <= now <= m.end + grace
        ]
        if running:
            return ("start", min(running, key=lambda m: m.start))

    if state == RecorderState.IDLE:
        # Schůzka začíná během arm_window_s.
        upcoming = [
            m
            for m in meetings
            if m.platform in _AUTO_PLATFORMS and now < m.start <= now + arm_window
        ]
        if upcoming:
            return ("arm", min(upcoming, key=lambda m: m.start))

    return ("none", None)
