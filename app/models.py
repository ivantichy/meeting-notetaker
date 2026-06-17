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
    #: Zobrazovaná jména účastníků (CN z kalendáře), když jsou k dispozici.
    #: Additivní pole — ``attendees`` (e-maily) zůstává kvůli zpětné kompatibilitě.
    #: Slouží k sestavení initial_prompt slovníku (lepší přepis jmen).
    attendee_names: "list[str]" = field(default_factory=list)
    #: Vyčištěný popis události z kalendáře (DESCRIPTION) — bez URL, e-mailů,
    #: telefonů a boilerplate (Teams/Zoom), zkrácený (~800 znaků). Additivní
    #: pole; chybějící popis -> "". Z něj se lokálně extrahují tematické termíny
    #: do initial_prompt (kontext konkrétní schůzky, lepší přepis názvů).
    description: str = ""

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
