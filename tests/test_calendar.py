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
        assert "síť" in svc.last_error

    def test_initial_state(self):
        svc = CalendarService(AppConfig())
        assert svc.meetings == []
        assert svc.last_error is None
