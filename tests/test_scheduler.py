"""Testy čisté plánovací logiky pick_action."""
from datetime import timedelta

import pytest

from app.config import AppConfig
from app.models import Platform, RecorderState
from app.scheduler import pick_action

CFG = AppConfig(arm_window_s=120, stop_grace_s=300)
ARM = timedelta(seconds=CFG.arm_window_s)
GRACE = timedelta(seconds=CFG.stop_grace_s)
SEC = timedelta(seconds=1)


class TestNone:
    def test_no_meetings(self, fixed_now):
        assert pick_action(fixed_now, [], RecorderState.IDLE, None, CFG) == (
            "none",
            None,
        )

    def test_meeting_far_in_future(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now + timedelta(hours=2))
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "none",
            None,
        )

    def test_just_outside_arm_window(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now + ARM + SEC)
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "none",
            None,
        )

    def test_finalizing_state_does_nothing(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now)  # právě začala
        assert pick_action(
            fixed_now, [m], RecorderState.FINALIZING, None, CFG
        ) == ("none", None)


class TestArm:
    def test_arm_within_window(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now + timedelta(seconds=60))
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == ("arm", m)

    def test_arm_exactly_at_window_edge(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now + ARM)  # začíná přesně za arm_window_s
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == ("arm", m)

    def test_armed_state_does_not_rearm(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now + timedelta(seconds=60))
        assert pick_action(fixed_now, [m], RecorderState.ARMED, m, CFG) == (
            "none",
            None,
        )

    def test_other_platform_never_armed(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now + timedelta(seconds=60), platform=Platform.OTHER)
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "none",
            None,
        )

    def test_teams_is_armed(self, fixed_now, make_meeting):
        m = make_meeting(
            fixed_now + timedelta(seconds=60),
            platform=Platform.TEAMS,
            join_url="https://teams.microsoft.com/l/meetup-join/abc",
        )
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == ("arm", m)


class TestStart:
    def test_start_exactly_at_start_from_armed(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now)
        assert pick_action(fixed_now, [m], RecorderState.ARMED, m, CFG) == ("start", m)

    def test_start_exactly_at_start_from_idle(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now)
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "start",
            m,
        )

    def test_restart_mid_meeting_idle_starts(self, fixed_now, make_meeting):
        # restart aplikace uprostřed schůzky: IDLE a now mezi start a end -> start
        m = make_meeting(fixed_now - timedelta(minutes=20), duration_min=60)
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "start",
            m,
        )

    def test_start_at_end_plus_grace_edge(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now - timedelta(minutes=60) - GRACE, duration_min=60)
        # now == m.end + grace -> ještě start (inkluzivní hranice)
        assert m.end + GRACE == fixed_now
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "start",
            m,
        )

    def test_no_start_after_end_plus_grace(self, fixed_now, make_meeting):
        m = make_meeting(
            fixed_now - timedelta(minutes=60) - GRACE - SEC, duration_min=60
        )
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "none",
            None,
        )

    def test_other_platform_never_started(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now, platform=Platform.OTHER)
        assert pick_action(fixed_now, [m], RecorderState.IDLE, None, CFG) == (
            "none",
            None,
        )

    def test_start_preferred_over_arm(self, fixed_now, make_meeting):
        running = make_meeting(fixed_now - timedelta(minutes=5), uid="running")
        soon = make_meeting(fixed_now + timedelta(seconds=60), uid="soon")
        action, m = pick_action(
            fixed_now, [soon, running], RecorderState.IDLE, None, CFG
        )
        assert (action, m.uid) == ("start", "running")


class TestStop:
    def test_no_stop_during_meeting(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now - timedelta(minutes=30), duration_min=60)
        assert pick_action(fixed_now, [m], RecorderState.RECORDING, m, CFG) == (
            "none",
            None,
        )

    def test_no_stop_exactly_at_end_plus_grace(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now - timedelta(minutes=60) - GRACE, duration_min=60)
        assert m.end + GRACE == fixed_now
        assert pick_action(fixed_now, [m], RecorderState.RECORDING, m, CFG) == (
            "none",
            None,
        )

    def test_stop_after_grace(self, fixed_now, make_meeting):
        m = make_meeting(
            fixed_now - timedelta(minutes=60) - GRACE - SEC, duration_min=60
        )
        assert pick_action(fixed_now, [m], RecorderState.RECORDING, m, CFG) == (
            "stop",
            m,
        )

    def test_stop_works_even_if_meeting_not_in_list(self, fixed_now, make_meeting):
        m = make_meeting(fixed_now - timedelta(hours=3), duration_min=60)
        assert pick_action(fixed_now, [], RecorderState.RECORDING, m, CFG) == (
            "stop",
            m,
        )

    def test_other_platform_can_be_stopped(self, fixed_now, make_meeting):
        # OTHER se nikdy auto-nestartuje, ale pokud se nějak nahrává, jde zastavit
        m = make_meeting(
            fixed_now - timedelta(hours=3), duration_min=60, platform=Platform.OTHER
        )
        assert pick_action(fixed_now, [m], RecorderState.RECORDING, m, CFG) == (
            "stop",
            m,
        )


class TestOverlap:
    def test_earliest_start_wins(self, fixed_now, make_meeting):
        a = make_meeting(fixed_now - timedelta(minutes=10), duration_min=60, uid="a")
        b = make_meeting(fixed_now - timedelta(minutes=5), duration_min=60, uid="b")
        action, m = pick_action(fixed_now, [b, a], RecorderState.IDLE, None, CFG)
        assert (action, m.uid) == ("start", "a")

    def test_earliest_start_wins_for_arm(self, fixed_now, make_meeting):
        a = make_meeting(fixed_now + timedelta(seconds=30), uid="a")
        b = make_meeting(fixed_now + timedelta(seconds=90), uid="b")
        action, m = pick_action(fixed_now, [b, a], RecorderState.IDLE, None, CFG)
        assert (action, m.uid) == ("arm", "a")

    def test_never_stop_early_for_next_meeting(self, fixed_now, make_meeting):
        current = make_meeting(
            fixed_now - timedelta(minutes=50), duration_min=60, uid="current"
        )
        nxt = make_meeting(fixed_now - timedelta(minutes=1), duration_min=60, uid="next")
        # nahráváme current, next už běží -> žádná akce (nezastavovat dřív)
        assert pick_action(
            fixed_now, [current, nxt], RecorderState.RECORDING, current, CFG
        ) == ("none", None)

    def test_next_meeting_starts_after_current_grace_expires(
        self, fixed_now, make_meeting
    ):
        current = make_meeting(
            fixed_now - timedelta(minutes=70), duration_min=60, uid="current"
        )
        nxt = make_meeting(fixed_now - timedelta(minutes=5), duration_min=60, uid="next")
        # 1) grace u current vypršela -> stop
        action, m = pick_action(
            fixed_now, [current, nxt], RecorderState.RECORDING, current, CFG
        )
        assert (action, m.uid) == ("stop", "current")
        # 2) další tick v IDLE -> start next
        action, m = pick_action(
            fixed_now, [current, nxt], RecorderState.IDLE, None, CFG
        )
        assert (action, m.uid) == ("start", "next")
