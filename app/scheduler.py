"""Plánovač: čisté funkce rozhodující o auto start/stop nahrávání.

Kromě hlavního ``pick_action`` (kalendářové okno) jsou zde i čisté pomocné
funkce pro logiku, která dřív žila zamotaná v UI a nešla testovat (H7):
``gate_start_on_call`` (počkat, až se hovor reálně rozběhne) a
``evaluate_calendar_call`` (early-stop / no-call-timeout u kalendářového
záznamu podle aktivity hovoru). Obojí je bez side-efektů a bez Qt.
"""
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


def gate_start_on_call(detect_calls: bool, call_active: bool) -> bool:
    """Má se kalendářový ``start`` zatím pozdržet (jen „arm") a počkat, až se
    hovor reálně rozběhne?  True = nezačínat naslepo v čase události a počkat
    na aktivní mikrofon (Teams/prohlížeč drží mikrofon). Bez detekce hovorů
    se startuje hned. (H7 — čistá, testovatelná verze gatingu z UI.)"""
    if not detect_calls:
        return False
    return not call_active


def evaluate_calendar_call(
    *,
    call_active: bool,
    call_seen: bool,
    secs_since_last_call: float,
    elapsed_s: float,
    early_stop_grace_s: float,
    no_call_timeout_s: float,
) -> "tuple[str, bool]":
    """Rozhodne osud BĚŽÍCÍHO kalendářového záznamu podle aktivity hovoru.

    Vrací ``(action, call_seen)`` kde action je:
      * ``"continue"``  — nahrávej dál (a aktualizuj call_seen),
      * ``"stop_early"``— hovor proběhl a pak skončil (mikrofon uvolněn déle
        než ``early_stop_grace_s``) → zastav dřív než v end+grace,
      * ``"stop_no_call"`` — žádný hovor se nerozběhl do ``no_call_timeout_s``
        (uživatel se k meetingu nepřipojil) → zastav.

    ``call_seen`` ve výstupu je aktualizovaný příznak „hovor už byl aspoň
    jednou aktivní" (volající si ho má uložit zpět). Čistá funkce — bez času,
    bez Qt; časové vstupy předává volající. (H7.)
    """
    if call_active:
        return ("continue", True)
    if call_seen:
        if secs_since_last_call > early_stop_grace_s:
            return ("stop_early", False)
        return ("continue", True)
    # hovor se zatím nikdy nerozběhl
    if elapsed_s > no_call_timeout_s:
        return ("stop_no_call", False)
    return ("continue", False)
