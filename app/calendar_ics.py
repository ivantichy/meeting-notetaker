"""ICS kalendář: stažení + parsování na list[Meeting].

Používá icalendar + recurring_ical_events pro rozbalení opakovaných událostí.
Celodenní události se přeskakují. Časy jsou tz-aware v lokální časové zóně.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import recurring_ical_events
from dateutil import tz
from icalendar import Calendar

from app.config import AppConfig
from app.models import Meeting, Platform

# meet.google.com/abc-defg-hij (+ případné query parametry)
_MEET_RE = re.compile(r"https?://meet\.google\.com/[A-Za-z0-9\-._]+(?:\?[^\s<>\"']*)?")
# teams.microsoft.com/l/meetup-join/... nebo teams.live.com/...
_TEAMS_RE = re.compile(
    r"https?://teams\.(?:microsoft\.com/l/meetup-join|live\.com)/[^\s<>\"']+"
)


def fetch_ics(url: str, timeout: int = 15) -> str:
    """Stáhne ICS text ze zadané (tajné) URL."""
    import requests  # lazy import — testy jej nepotřebují

    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _to_local(dt: datetime) -> datetime:
    """Převede datetime na tz-aware lokální čas."""
    local = tz.tzlocal()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=local)
    return dt.astimezone(local)


def _event_texts(event) -> "list[str]":
    """Posbírá textové vlastnosti, ve kterých hledáme odkaz na schůzku."""
    texts = []
    for key in ("LOCATION", "DESCRIPTION", "X-GOOGLE-CONFERENCE", "URL"):
        value = event.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            texts.extend(str(v) for v in value)
        else:
            texts.append(str(value))
    return texts


def _detect_platform(event) -> "tuple[Platform, str | None]":
    """Najde platformu a plnou join URL v LOCATION/DESCRIPTION/X-GOOGLE-CONFERENCE/URL."""
    for text in _event_texts(event):
        m = _MEET_RE.search(text)
        if m:
            return Platform.MEET, m.group(0).rstrip(".,;)>")
        m = _TEAMS_RE.search(text)
        if m:
            return Platform.TEAMS, m.group(0).rstrip(".,;)>")
    return Platform.OTHER, None


def _attendees(event) -> "list[str]":
    value = event.get("ATTENDEE")
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    out = []
    for a in value:
        s = str(a)
        if s.lower().startswith("mailto:"):
            s = s[len("mailto:"):]
        if s:
            out.append(s)
    return out


def parse_meetings(ics_text: str, window_days: int = 7) -> "list[Meeting]":
    """Expand recurring events (recurring_ical_events) from now-12h to now+window_days.

    Detect platform/join_url from LOCATION, DESCRIPTION, X-GOOGLE-CONFERENCE:
      meet.google.com/* -> MEET ; teams.microsoft.com/l/meetup-join or teams.live.com -> TEAMS.
    Sort by start. Celodenní události (DTSTART bez času) se přeskakují.
    """
    cal = Calendar.from_ical(ics_text)
    now = datetime.now(tz.tzlocal())
    window_start = now - timedelta(hours=12)
    window_end = now + timedelta(days=window_days)

    occurrences = recurring_ical_events.of(cal).between(window_start, window_end)

    meetings: "list[Meeting]" = []
    for event in occurrences:
        dtstart = event.get("DTSTART")
        if dtstart is None:
            continue
        start_raw = dtstart.dt
        if not isinstance(start_raw, datetime):
            # celodenní událost (jen datum) -> ignorovat
            continue
        start = _to_local(start_raw)

        dtend = event.get("DTEND")
        if dtend is not None and isinstance(dtend.dt, datetime):
            end = _to_local(dtend.dt)
        else:
            end = start + timedelta(hours=1)

        platform, join_url = _detect_platform(event)
        ics_uid = str(event.get("UID", ""))
        title = str(event.get("SUMMARY", "")) or "Bez názvu"

        meetings.append(
            Meeting(
                uid=f"{ics_uid}:{start.isoformat()}",
                title=title,
                start=start,
                end=end,
                platform=platform,
                join_url=join_url,
                attendees=_attendees(event),
            )
        )

    meetings.sort(key=lambda m: m.start)
    return meetings


class CalendarService:
    """Drží poslední dobrý seznam schůzek; UI jej polluje. Bez Qt závislostí."""

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._meetings: "list[Meeting]" = []
        self._last_error: "str | None" = None

    def refresh(self) -> "list[Meeting]":
        """Stáhne a naparsuje ICS; při chybě sítě ponechá poslední dobrý výsledek."""
        try:
            text = fetch_ics(self._cfg.ics_url)
            self._meetings = parse_meetings(text)
            self._last_error = None
        except Exception as exc:  # network / parse error -> keep last good
            self._last_error = str(exc)
        return self._meetings

    @property
    def meetings(self) -> "list[Meeting]":
        return self._meetings

    @property
    def last_error(self) -> "str | None":
        return self._last_error
