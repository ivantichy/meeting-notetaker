"""Sdílené fixtures pro testy.

DŮLEŽITÉ: mocky pro 'soundcard' a 'faster_whisper' se instalují do sys.modules
hned při importu tohoto souboru — dřív, než se importuje cokoliv z app/ —
takže testy nikdy nenačtou skutečné audio/whisper knihovny (běží i na Linuxu).
"""
import os
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("soundcard", MagicMock())
sys.modules.setdefault("faster_whisper", MagicMock())

from datetime import datetime, timedelta, timezone  # noqa: E402

import pytest  # noqa: E402
from dateutil import tz  # noqa: E402

from app.models import Meeting, Platform  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """Každý test běží v izolovaném pracovním adresáři.

    Některý produkční kód sahá na relativní soubory v pracovním adresáři appky
    (např. ``glossary.txt`` vedle ``config.json``). Bez izolace by je testy
    zakládaly v kořeni repozitáře. Přesměrujeme cwd do ``tmp_path`` — žádný test
    se na konkrétní cwd nespoléhá (cesty jsou absolutní nebo z ``__file__``).
    """
    monkeypatch.chdir(tmp_path)


def _ics_dt(dt: datetime) -> str:
    """Datetime -> ICS UTC formát YYYYMMDDTHHMMSSZ."""
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _wrap_calendar(*events: str) -> str:
    return "\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//test//meeting-notetaker//CS",
            *events,
            "END:VCALENDAR",
        ]
    )


@pytest.fixture
def now_local() -> datetime:
    """Aktuální tz-aware lokální čas (bez mikrosekund kvůli ICS zaokrouhlení)."""
    return datetime.now(tz.tzlocal()).replace(microsecond=0)


@pytest.fixture
def fixed_now() -> datetime:
    """Pevný tz-aware čas pro deterministické testy scheduleru."""
    return datetime(2026, 6, 12, 13, 0, 0, tzinfo=tz.gettz("Europe/Prague"))


def _single_meet_event(now: datetime) -> str:
    start = now + timedelta(hours=2)
    end = start + timedelta(hours=1)
    return "\n".join(
        [
            "BEGIN:VEVENT",
            "UID:single-meet-1@example.com",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART:{_ics_dt(start)}",
            f"DTEND:{_ics_dt(end)}",
            "SUMMARY:Týmová porada",
            "DESCRIPTION:Připojte se zde: https://meet.google.com/abc-defg-hij",
            "X-GOOGLE-CONFERENCE:https://meet.google.com/abc-defg-hij",
            "ATTENDEE;CN=Ivan:mailto:ivan@example.com",
            "ATTENDEE;CN=Petr:mailto:petr@example.com",
            "END:VEVENT",
        ]
    )


def _weekly_teams_event(now: datetime) -> str:
    """Týdenní opakovaná schůzka; první výskyt před 6 dny -> další za 1 den."""
    first = (now - timedelta(days=6)).replace(minute=0, second=0)
    return "\n".join(
        [
            "BEGIN:VEVENT",
            "UID:weekly-teams-1@example.com",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART:{_ics_dt(first)}",
            f"DTEND:{_ics_dt(first + timedelta(minutes=30))}",
            "RRULE:FREQ=WEEKLY",
            "SUMMARY:Týdenní standup",
            "LOCATION:https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"
            "%40thread.v2/0?context=%7b%22Tid%22%3a%22xyz%22%7d",
            "END:VEVENT",
        ]
    )


def _allday_event(now: datetime) -> str:
    day = (now + timedelta(days=1)).date()
    return "\n".join(
        [
            "BEGIN:VEVENT",
            "UID:allday-1@example.com",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + timedelta(days=1)).strftime('%Y%m%d')}",
            "SUMMARY:Státní svátek",
            "END:VEVENT",
        ]
    )


def _no_link_event(now: datetime) -> str:
    start = now + timedelta(hours=3)
    return "\n".join(
        [
            "BEGIN:VEVENT",
            "UID:nolink-1@example.com",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART:{_ics_dt(start)}",
            f"DTEND:{_ics_dt(start + timedelta(hours=1))}",
            "SUMMARY:Oběd s týmem",
            "LOCATION:Kancelář 4.12",
            "END:VEVENT",
        ]
    )


def _past_event(now: datetime) -> str:
    """Schůzka skončila před 2 dny — mimo okno now-12h."""
    start = now - timedelta(days=2)
    return "\n".join(
        [
            "BEGIN:VEVENT",
            "UID:past-1@example.com",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART:{_ics_dt(start)}",
            f"DTEND:{_ics_dt(start + timedelta(hours=1))}",
            "SUMMARY:Stará schůzka",
            "DESCRIPTION:https://meet.google.com/old-oldd-old",
            "END:VEVENT",
        ]
    )


def _far_future_event(now: datetime) -> str:
    """Schůzka za 9 dní — mimo okno window_days=7."""
    start = now + timedelta(days=9)
    return "\n".join(
        [
            "BEGIN:VEVENT",
            "UID:future-1@example.com",
            f"DTSTAMP:{_ics_dt(now)}",
            f"DTSTART:{_ics_dt(start)}",
            f"DTEND:{_ics_dt(start + timedelta(hours=1))}",
            "SUMMARY:Daleká budoucnost",
            "DESCRIPTION:https://meet.google.com/far-futur-eee",
            "END:VEVENT",
        ]
    )


@pytest.fixture
def ics_single_meet(now_local) -> str:
    """Jedna událost s Google Meet URL v X-GOOGLE-CONFERENCE i DESCRIPTION."""
    return _wrap_calendar(_single_meet_event(now_local))


@pytest.fixture
def ics_weekly_teams(now_local) -> str:
    """Týdenní opakovaná událost s Teams meetup-join URL v LOCATION."""
    return _wrap_calendar(_weekly_teams_event(now_local))


@pytest.fixture
def ics_allday(now_local) -> str:
    """Celodenní událost — musí být ignorována."""
    return _wrap_calendar(_allday_event(now_local))


@pytest.fixture
def ics_no_link(now_local) -> str:
    """Událost bez odkazu na schůzku -> Platform.OTHER."""
    return _wrap_calendar(_no_link_event(now_local))


@pytest.fixture
def ics_full(now_local) -> str:
    """Kombinovaný kalendář: všechny typy + událost mimo okno (minulost i budoucnost)."""
    return _wrap_calendar(
        _no_link_event(now_local),
        _single_meet_event(now_local),
        _weekly_teams_event(now_local),
        _allday_event(now_local),
        _past_event(now_local),
        _far_future_event(now_local),
    )


@pytest.fixture
def tmp_notes_dir(tmp_path) -> str:
    """Dočasný adresář pro poznámky."""
    d = tmp_path / "notes"
    return str(d)


@pytest.fixture
def make_meeting():
    """Factory na Meeting objekty pro testy scheduleru/storage."""

    def _make(
        start: datetime,
        duration_min: int = 60,
        platform: Platform = Platform.MEET,
        title: str = "Testovací schůzka",
        uid: "str | None" = None,
        join_url: "str | None" = "https://meet.google.com/abc-defg-hij",
        attendees: "list[str] | None" = None,
    ) -> Meeting:
        end = start + timedelta(minutes=duration_min)
        return Meeting(
            uid=uid or f"test-uid:{start.isoformat()}",
            title=title,
            start=start,
            end=end,
            platform=platform,
            join_url=join_url,
            attendees=attendees or [],
        )

    return _make


@pytest.fixture
def sample_meeting(fixed_now, make_meeting) -> Meeting:
    """Vzorová schůzka (Meet, 60 min) začínající v fixed_now."""
    return make_meeting(
        fixed_now,
        title="Týmová porada č. 5",
        attendees=["ivan@example.com", "petr@example.com"],
    )
