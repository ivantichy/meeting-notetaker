"""Detekce probíhajícího hovoru podle využití mikrofonu (Windows).

Windows eviduje v registru (CapabilityAccessManager / ConsentStore), která
aplikace právě používá mikrofon: položka s ``LastUsedTimeStop == 0`` znamená
"používá mikrofon právě teď". Sledujeme aplikace, ve kterých běží hovory
(Teams, prohlížeče s Google Meet) — když některá začne používat mikrofon,
považujeme to za probíhající hovor.

``winreg`` se importuje líně, takže modul je bezpečně importovatelný
i na Linuxu (testy předávají vlastní ``entries``).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

#: exe -> (popisek hovoru, priorita; nižší = vyšší priorita)
WATCHED_APPS: dict[str, tuple[str, int]] = {
    "ms-teams.exe": ("Teams hovor", 0),
    "teams.exe": ("Teams hovor", 0),
    "chrome.exe": ("Hovor v Chrome (Meet)", 1),
    "msedge.exe": ("Hovor v Edge", 2),
    "firefox.exe": ("Hovor ve Firefoxu", 2),
}

_MIC_KEY = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion"
    r"\CapabilityAccessManager\ConsentStore\microphone"
)


def detect_label(entries: list[tuple[str, int]]) -> str | None:
    """Čistá logika (testovatelná): z dvojic ``(identifikátor, last_stop)``
    vybere popisek probíhajícího hovoru, nebo ``None``.

    ``identifikátor`` je název exe (NonPackaged) nebo název balíčku
    (packaged aplikace, např. ``MSTeams_8wekyb3d8bbwe``).
    ``last_stop == 0`` znamená, že aplikace mikrofon právě používá.
    """
    best: tuple[int, str] | None = None
    for ident, last_stop in entries:
        if last_stop != 0:
            continue
        name = ident.lower()
        exe = name.rsplit("#", 1)[-1]  # NonPackaged klíče: C:#Users#...#app.exe
        if exe in WATCHED_APPS:
            label, prio = WATCHED_APPS[exe]
        elif "teams" in name:  # packaged Teams (MSTeams_...)
            label, prio = "Teams hovor", 0
        else:
            continue
        if best is None or prio < best[0]:
            best = (prio, label)
    return best[1] if best else None


def _read_entries() -> list[tuple[str, int]]:
    """Načte z registru všechny mikrofoní položky jako ``(ident, last_stop)``."""
    import winreg

    entries: list[tuple[str, int]] = []

    def _scan(root_key, yield_prefix: str = "") -> None:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(root_key, i)
            except OSError:
                break
            i += 1
            if sub == "NonPackaged":
                try:
                    with winreg.OpenKey(root_key, sub) as np_key:
                        _scan(np_key, yield_prefix="np:")
                except OSError:
                    continue
                continue
            try:
                with winreg.OpenKey(root_key, sub) as app_key:
                    stop, _type = winreg.QueryValueEx(app_key, "LastUsedTimeStop")
                    entries.append((yield_prefix + sub, int(stop)))
            except OSError:
                continue

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _MIC_KEY) as mic_key:
            _scan(mic_key)
    except OSError:
        log.warning("Nelze číst registr mikrofonu (ConsentStore).")
    return entries


def active_call() -> str | None:
    """Vrátí popisek probíhajícího hovoru (např. "Teams hovor"), nebo None."""
    try:
        return detect_label(_read_entries())
    except Exception:  # noqa: BLE001 - detekce nesmí shodit aplikaci
        log.exception("Detekce hovoru selhala.")
        return None
