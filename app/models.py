"""Datové modely aplikace: Meeting a stavy rekordéru."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

SLUG_MAX_LEN = 60


class Platform(str, Enum):
    MEET = "meet"
    TEAMS = "teams"
    OTHER = "other"


class RecorderState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"          # meeting starts within arm_window
    RECORDING = "recording"
    FINALIZING = "finalizing"


@dataclass
class Meeting:
    uid: str                 # ICS UID + start isoformat (recurrence-safe)
    title: str
    start: datetime          # tz-aware, local tz
    end: datetime
    platform: Platform
    join_url: "str | None" = None
    attendees: "list[str]" = field(default_factory=list)

    @property
    def slug(self) -> str:
        """Souborové jméno: '2026-06-12_1330_nazev-meetingu' (ascii, max 60 znaků)."""
        prefix = self.start.strftime("%Y-%m-%d_%H%M") + "_"
        title = unicodedata.normalize("NFKD", self.title)
        title = title.encode("ascii", "ignore").decode("ascii").lower()
        title = re.sub(r"[^a-z0-9]+", "-", title).strip("-")
        if not title:
            title = "meeting"
        return (prefix + title)[:SLUG_MAX_LEN].rstrip("-_")
