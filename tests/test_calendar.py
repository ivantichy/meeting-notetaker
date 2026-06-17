"""Testy parsování ICS kalendáře."""
from datetime import datetime, timedelta

import pytest
from dateutil import tz

from app.calendar_ics import CalendarService, parse_meetings
from app.config import AppConfig
from app.models import Platform


class TestSingleMeetEvent:
    def test_parses_one_meeting(self, ics_single_meet):
        meetings = parse_meetings(ics_single_meet)
        assert len(meetings) == 1

    def test_platform_and_url_from_x_google_conference(self, ics_single_meet):
        (m,) = parse_meetings(ics_single_meet)
        assert m.platform == Platform.MEET
        assert m.join_url == "https://meet.google.com/abc-defg-hij"

    def test_title_keeps_diacritics(self, ics_single_meet):
        (m,) = parse_meetings(ics_single_meet)
        assert m.title == "Týmová porada"

    def test_uid_contains_ics_uid_and_start_isoformat(self, ics_single_meet):
        (m,) = parse_meetings(ics_single_meet)
        assert m.uid == f"single-meet-1@example.com:{m.start.isoformat()}"

    def test_times_are_tz_aware_local(self, ics_single_meet, now_local):
        (m,) = parse_meetings(ics_single_meet)
        assert m.start.tzinfo is not None
        assert m.end.tzinfo is not None
        # událost byla vytvořena na now+2h / délka 1h (UTC v ICS, lokální po parse)
        assert abs((m.start - (now_local + timedelta(hours=2))).total_seconds()) < 2
        assert m.end - m.start == timedelta(hours=1)
        # offset odpovídá lokální zóně
        assert m.start.utcoffset() == m.start.astimezone(tz.tzlocal()).utcoffset()

    def test_attendees_parsed_without_mailto(self, ics_single_meet):
        (m,) = parse_meetings(ics_single_meet)
        assert "ivan@example.com" in m.attendees
        assert "petr@example.com" in m.attendees

    def test_attendee_names_parsed_from_cn(self, ics_single_meet):
        """CN (zobrazované jméno) z ATTENDEE se naparsuje do attendee_names."""
        (m,) = parse_meetings(ics_single_meet)
        assert m.attendee_names == ["Ivan", "Petr"]
        # e-maily zůstávají v attendees (zpětná kompatibilita)
        assert m.attendees == ["ivan@example.com", "petr@example.com"]


class TestAttendeeNamesFallback:
    """CN -> attendee_names; chybějící CN -> lokální část e-mailu."""

    def _event(self, now) -> str:
        from datetime import timezone

        def _z(dt):
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        start = now + timedelta(hours=1)
        return "\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//test//x//CS",
                "BEGIN:VEVENT",
                "UID:names-1@example.com",
                f"DTSTART:{_z(start)}",
                f"DTEND:{_z(start + timedelta(hours=1))}",
                "SUMMARY:Schůzka se jmény",
                "DESCRIPTION:https://meet.google.com/aaa-bbbb-ccc",
                "ATTENDEE;CN=Jana Nováková:mailto:jana@example.com",
                "ATTENDEE:mailto:karel@example.com",  # bez CN -> lokální část
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )

    def test_cn_used_and_email_local_part_fallback(self, now_local):
        (m,) = parse_meetings(self._event(now_local))
        # první má CN, druhý padá na lokální část e-mailu
        assert m.attendee_names == ["Jana Nováková", "karel"]
        assert m.attendees == ["jana@example.com", "karel@example.com"]


class TestRecurringTeamsEvent:
    def test_expands_single_occurrence_in_default_window(self, ics_weekly_teams):
        meetings = parse_meetings(ics_weekly_teams, window_days=7)
        # první výskyt byl před 6 dny (mimo okno), další za 1 den
        assert len(meetings) == 1

    def test_expands_multiple_occurrences_in_wider_window(self, ics_weekly_teams):
        meetings = parse_meetings(ics_weekly_teams, window_days=14)
        assert len(meetings) == 2
        assert meetings[1].start - meetings[0].start == timedelta(days=7)

    def test_occurrences_have_distinct_uids(self, ics_weekly_teams):
        meetings = parse_meetings(ics_weekly_teams, window_days=14)
        uids = {m.uid for m in meetings}
        assert len(uids) == 2
        for m in meetings:
            assert m.uid.startswith("weekly-teams-1@example.com:")

    def test_platform_teams_from_location(self, ics_weekly_teams):
        meetings = parse_meetings(ics_weekly_teams)
        m = meetings[0]
        assert m.platform == Platform.TEAMS
        assert m.join_url is not None
        assert m.join_url.startswith(
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc"
        )
        # plná URL včetně query parametrů
        assert "context=" in m.join_url


class TestSpecialCases:
    def test_allday_event_is_ignored(self, ics_allday):
        assert parse_meetings(ics_allday) == []

    def test_event_without_link_is_platform_other(self, ics_no_link):
        (m,) = parse_meetings(ics_no_link)
        assert m.platform == Platform.OTHER
        assert m.join_url is None
        assert m.title == "Oběd s týmem"

    def test_window_filters_past_and_far_future(self, ics_full):
        meetings = parse_meetings(ics_full, window_days=7)
        uids = {m.uid.split(":")[0] for m in meetings}
        assert "past-1@example.com" not in uids       # skončila před 2 dny
        assert "future-1@example.com" not in uids     # začíná za 9 dní
        assert "allday-1@example.com" not in uids     # celodenní
        assert uids == {
            "single-meet-1@example.com",
            "weekly-teams-1@example.com",
            "nolink-1@example.com",
        }

    def test_sorted_by_start(self, ics_full):
        meetings = parse_meetings(ics_full)
        starts = [m.start for m in meetings]
        assert starts == sorted(starts)


class TestCalendarService:
    def test_refresh_success(self, monkeypatch, ics_single_meet):
        cfg = AppConfig(ics_url="https://example.com/secret.ics")
        svc = CalendarService(cfg)
        monkeypatch.setattr(
            "app.calendar_ics.fetch_ics", lambda url, timeout=15: ics_single_meet
        )
        result = svc.refresh()
        assert len(result) == 1
        assert svc.meetings == result
        assert svc.last_error is None

    def test_refresh_keeps_last_good_on_network_error(
        self, monkeypatch, ics_single_meet
    ):
        cfg = AppConfig(ics_url="https://example.com/secret.ics")
        svc = CalendarService(cfg)
        monkeypatch.setattr(
            "app.calendar_ics.fetch_ics", lambda url, timeout=15: ics_single_meet
        )
        svc.refresh()
        good = list(svc.meetings)
        assert len(good) == 1

        def _boom(url, timeout=15):
            raise ConnectionError("síť nedostupná")

        monkeypatch.setattr("app.calendar_ics.fetch_ics", _boom)
        result = svc.refresh()
        assert result == good            # poslední dobrý výsledek zůstal
        assert svc.meetings == good
        assert svc.last_error is not None
        # chyba je bezpečná zpráva bez detailu výjimky a hlavně bez URL (C1)
        assert "secret" not in svc.last_error
        assert "ConnectionError" in svc.last_error

    def test_initial_state(self):
        svc = CalendarService(AppConfig())
        assert svc.meetings == []
        assert svc.last_error is None


# ------------------------------------------- C1: tajná URL nesmí uniknout


_SECRET_URL = "https://calendar.example.com/private/SECRETTOKEN123/basic.ics"


class _FakeResp:
    def __init__(self, status_code=200, text="", is_redirect=False):
        self.status_code = status_code
        self.text = text
        self.is_redirect = is_redirect
        self.is_permanent_redirect = False


class TestFetchIcsSecurity:
    def test_network_error_does_not_leak_url(self, monkeypatch):
        import requests

        from app import calendar_ics

        def _boom(url, **kw):
            # reálná requests výjimka nese celou URL ve zprávě
            raise requests.exceptions.ConnectionError(
                f"Failed to establish connection to {url}"
            )

        monkeypatch.setattr(requests, "get", _boom)
        with pytest.raises(calendar_ics.CalendarError) as ei:
            calendar_ics.fetch_ics(_SECRET_URL)
        msg = str(ei.value)
        assert "SECRETTOKEN123" not in msg
        assert _SECRET_URL not in msg

    def test_http_error_does_not_leak_url(self, monkeypatch):
        import requests

        from app import calendar_ics

        monkeypatch.setattr(requests, "get", lambda url, **kw: _FakeResp(status_code=404))
        with pytest.raises(calendar_ics.CalendarError) as ei:
            calendar_ics.fetch_ics(_SECRET_URL)
        assert "404" in str(ei.value)
        assert "SECRETTOKEN123" not in str(ei.value)

    def test_refresh_last_error_never_contains_url(self, monkeypatch):
        import requests

        cfg = AppConfig(ics_url=_SECRET_URL)
        svc = CalendarService(cfg)

        def _boom(url, **kw):
            raise requests.exceptions.Timeout(f"timeout for {url}")

        monkeypatch.setattr(requests, "get", _boom)
        svc.refresh()
        assert svc.last_error is not None
        assert "SECRETTOKEN123" not in svc.last_error
        assert _SECRET_URL not in svc.last_error


class TestFetchIcsRedirects:
    def test_redirect_is_rejected_not_followed(self, monkeypatch):
        import requests

        from app import calendar_ics

        called = {}

        def _get(url, **kw):
            called.update(kw)
            return _FakeResp(status_code=302, is_redirect=True)

        monkeypatch.setattr(requests, "get", _get)
        with pytest.raises(calendar_ics.CalendarError):
            calendar_ics.fetch_ics(_SECRET_URL)
        # M1: redirecty se zásadně nenásledují, TLS ověření zapnuté
        assert called.get("allow_redirects") is False
        assert called.get("verify") is True

    def test_success_returns_text(self, monkeypatch):
        import requests

        from app import calendar_ics

        monkeypatch.setattr(
            requests,
            "get",
            lambda url, **kw: _FakeResp(status_code=200, text="BEGIN:VCALENDAR"),
        )
        assert calendar_ics.fetch_ics(_SECRET_URL) == "BEGIN:VCALENDAR"


# ------------------------------------------- M3: vadná událost neshodí parse


class TestParseRobustness:
    def test_valid_events_parse_alongside_skippable_ones(self, ics_full):
        # ics_full obsahuje i celodenní událost (přeskočí se) — ostatní validní
        # se musí naparsovat, parse se kvůli „divné" události nezhroutí (M3).
        meetings = parse_meetings(ics_full, window_days=7)
        uids = {m.uid.split(":")[0] for m in meetings}
        assert "single-meet-1@example.com" in uids
        assert "weekly-teams-1@example.com" in uids
        assert "allday-1@example.com" not in uids  # celodenní přeskočena

    def test_malformed_calendar_does_not_crash_refresh(self, monkeypatch):
        # Kalendář nečitelný i na úrovni parseru: refresh nesmí spadnout,
        # ponechá poslední dobrý výsledek a nastaví bezpečnou chybu (M3 + C1).
        cfg = AppConfig(ics_url="https://example.com/secret.ics")
        svc = CalendarService(cfg)
        monkeypatch.setattr(
            "app.calendar_ics.fetch_ics", lambda url, timeout=15: "TOTO NENÍ ICS"
        )
        result = svc.refresh()
        assert result == []  # žádná schůzka, ale bez pádu
        assert svc.last_error is not None
        assert "secret" not in svc.last_error

    def test_dtend_before_dtstart_yields_positive_duration(self, now_local):
        from datetime import timezone

        def _z(dt):
            return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        start = now_local + timedelta(hours=1)
        bad_end = start - timedelta(minutes=30)  # konec PŘED začátkem
        ics = "\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "PRODID:-//test//x//CS",
                "BEGIN:VEVENT",
                "UID:negdur-1@example.com",
                f"DTSTART:{_z(start)}",
                f"DTEND:{_z(bad_end)}",
                "SUMMARY:Záporné trvání",
                "DESCRIPTION:https://meet.google.com/aaa-bbbb-ccc",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        )
        (m,) = parse_meetings(ics)
        # Po našem guardu (a normalizaci knihovny) musí být trvání vždy kladné —
        # ať se na to scheduler může spolehnout.
        assert m.end > m.start
