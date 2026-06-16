"""ICS kalendář: stažení + parsování na list[Meeting].

Používá icalendar + recurring_ical_events pro rozbalení opakovaných událostí.
Celodenní události se přeskakují. Časy jsou tz-aware v lokální časové zóně.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import recurring_ical_events
from dateutil import tz
from icalendar import Calendar

from app.config import AppConfig
from app.models import Meeting, Platform

log = logging.getLogger(__name__)


class CalendarError(Exception):
    """Chyba kalendáře BEZ tajné ICS URL — bezpečná pro UI i log (C1).

    Výjimky z ``requests``/``urllib3`` totiž nesou celou URL včetně tajného
    tokenu; tu nesmíme nikdy pustit do status baru ani do notetaker.log.
    """

# meet.google.com/abc-defg-hij (+ případné query parametry)
_MEET_RE = re.compile(r"https?://meet\.google\.com/[A-Za-z0-9\-._]+(?:\?[^\s<>\"']*)?")
# teams.microsoft.com/l/meetup-join/... nebo teams.live.com/...
_TEAMS_RE = re.compile(
    r"https?://teams\.(?:microsoft\.com/l/meetup-join|live\.com)/[^\s<>\"']+"
)


def fetch_ics(url: str, timeout: int = 15) -> str:
    """Stáhne ICS text ze zadané (tajné) URL.

    Při jakékoli chybě vyhodí ``CalendarError`` BEZ URL (C1) — výjimky z
    requests/urllib3 jinak obsahují celou adresu včetně tajného tokenu.
    Přesměrování (3xx) se NEnásleduje (M1): tajemství by mohlo uniknout na
    jiný host nebo přejít na nešifrované http. TLS ověření je explicitně
    zapnuté.
    """
    import requests  # lazy import — testy jej nepotřebují

    try:
        resp = requests.get(
            url, timeout=timeout, allow_redirects=False, verify=True
        )
    except requests.exceptions.RequestException as exc:
        # NIKDY nepouštět str(exc) / URL ven — jen typ chyby.
        raise CalendarError(
            f"Síťová chyba při stahování kalendáře ({type(exc).__name__})."
        ) from None
    if resp.is_redirect or resp.is_permanent_redirect or 300 <= resp.status_code < 400:
        raise CalendarError(
            f"Kalendář vrátil přesměrování (HTTP {resp.status_code}); "
            "přesměrování se z bezpečnostních důvodů nenásleduje."
        ) from None
    if resp.status_code >= 400:
        raise CalendarError(f"Kalendář vrátil chybu HTTP {resp.status_code}.") from None
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
        # M3: jedna vadná (typicky rekurentní) událost nesmí shodit parsování
        # celého kalendáře — jinak by se kalendář tiše přestal aktualizovat.
        try:
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
            # M3: ochrana proti nevalidnímu/nulovému trvání (DTEND <= DTSTART).
            if end <= start:
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
        except Exception:  # noqa: BLE001 - přeskočit vadnou událost, ostatní zpracovat
            log.warning("Přeskakuji nečitelnou událost v kalendáři.", exc_info=True)
            continue

    meetings.sort(key=lambda m: m.start)
    return meetings


class CalendarService:
    """Drží poslední dobrý seznam schůzek; UI jej polluje. Bez Qt závislostí."""

    def __init__(self, cfg: AppConfig):
        self._cfg = cfg
        self._meetings: "list[Meeting]" = []
        self._last_error: "str | None" = None

    def refresh(self) -> "list[Meeting]":
        """Stáhne a naparsuje ICS; při chybě sítě ponechá poslední dobrý výsledek.

        ``_last_error`` nikdy neobsahuje ICS URL (C1): u ``CalendarError`` je
        zpráva už bezpečná (sestavená bez URL), u ostatních výjimek ukládáme
        jen typ chyby. Logujeme rovněž jen typ — ne ``str(exc)``.
        """
        try:
            text = fetch_ics(self._cfg.ics_url)
            self._meetings = parse_meetings(text)
            self._last_error = None
        except CalendarError as exc:
            self._last_error = str(exc)  # bez URL (viz fetch_ics)
            log.warning("Aktualizace kalendáře selhala: %s", type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - parse apod.; NIKDY nepouštět detail
            self._last_error = (
                f"Kalendář se nepodařilo zpracovat ({type(exc).__name__})."
            )
            log.warning(
                "Aktualizace kalendáře selhala: %s", type(exc).__name__
            )
        return self._meetings

    @property
    def meetings(self) -> "list[Meeting]":
        return self._meetings

    @property
    def last_error(self) -> "str | None":
        return self._last_error
