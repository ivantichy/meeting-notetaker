"""Publikace „locatoru" aplikace na PEVNÉ, instalaci-nezávislé cestě.

Problém: dev build píše poznámky do ``C:\\temp\\Claude\\meeting-notetaker\\notes``,
zatímco nainstalovaný build do ``%LOCALAPPDATA%\\Programs\\MeetingNotetaker\\notes``.
Jakýkoliv skill/task, který si jednu z cest zadrátuje, mine přepisy toho druhého
buildu.

Řešení: při startu zapíšeme malý JSON na JEDNU známou cestu nezávislou na tom,
kam je appka nainstalovaná — ``%LOCALAPPDATA%\\MeetingNotetaker\\app-info.json``
(stejný base dir jako single-instance zámek v ``main.py``). Skill pak čte ten
jediný soubor a z něj ``notes_dir``/``index`` — ať běží dev nebo instalace.

Zápis je plně defenzivní: jakákoliv chyba (chybějící ``LOCALAPPDATA``, nezapsatelný
adresář, …) se jen zaloguje a NIKDY neshodí start aplikace.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: Krátký popis formátu poznámek (orientace pro čtenáře app-info.json).
TRANSCRIPT_FORMAT = (
    "Markdown + YAML frontmatter; sections '## Přepis' with [HH:MM:SS] lines"
)


def _base_dir() -> "str | None":
    """Vrátí ``%LOCALAPPDATA%\\MeetingNotetaker`` — stejný base dir jako zámek
    v main.py. Když ``LOCALAPPDATA`` chybí, vrátí ``None`` (zápis se přeskočí)."""
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return None
    return os.path.join(local, "MeetingNotetaker")


def app_info_path() -> "str | None":
    """Absolutní cesta k app-info.json, nebo ``None`` když nelze určit base dir."""
    base = _base_dir()
    if base is None:
        return None
    return os.path.join(base, "app-info.json")


def write_app_info(cfg) -> None:
    """Zapíše/přepíše ``%LOCALAPPDATA%\\MeetingNotetaker\\app-info.json`` s
    absolutními cestami k poznámkám tohoto běhu (locator pro skilly/tasky).

    Plně defenzivní: chybějící ``LOCALAPPDATA`` -> tiše přeskočí; jakákoliv jiná
    chyba se zaloguje a spolkne. Start aplikace tato funkce NIKDY neshodí.
    """
    try:
        path = app_info_path()
        if path is None:
            log.info("app-info.json přeskočeno: LOCALAPPDATA není nastaven.")
            return
        # notes_dir z configu je relativní ke kořeni appky (main.py dělá chdir),
        # proto ho zafixujeme na ABSOLUTNÍ cestu — skill nesmí záviset na cwd.
        notes_dir = os.path.abspath(cfg.notes_dir)
        app_dir = os.path.abspath(os.getcwd())
        index_path = os.path.join(notes_dir, "index.jsonl")
        payload = {
            "app": "Meeting Notetaker",
            "notes_dir": notes_dir,
            "app_dir": app_dir,
            "index": index_path,
            "transcripts": True,
            "transcript_format": TRANSCRIPT_FORMAT,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        base = os.path.dirname(path)
        os.makedirs(base, exist_ok=True)
        # Atomický zápis (temp + rename) ve stejném adresáři — paralelní čtenář
        # nikdy neuvidí napůl zapsaný JSON.
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix="app-info.", suffix=".tmp", dir=base)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            # Když rename selhal, ukliď dočasný soubor (best-effort).
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
        log.info("Zapsán locator app-info.json: %s (notes_dir=%s)", path, notes_dir)
    except Exception:  # noqa: BLE001 — locator nesmí nikdy shodit start
        log.warning("Zápis app-info.json selhal (ignoruji).", exc_info=True)
