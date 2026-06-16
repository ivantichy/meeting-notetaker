"""Jednoduchý lidsky čitelný deník hovorů (notes/hovory.log).

Na rozdíl od notetaker.log (technický log) obsahuje jen události záznamů:
start/konec nahrávání, průběh dopřepisování z WAV. Jeden řádek na událost.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime

log = logging.getLogger(__name__)


class EventLog:
    """Append-only deník událostí. Vlákno-bezpečný, chyby jen loguje."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()

    def log(self, event: str, detail: str = "") -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{stamp} | {event:<16} | {detail}\n"
        try:
            with self._lock:
                os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:  # noqa: BLE001 - deník nesmí shodit aplikaci
            log.exception("Zápis do deníku hovorů selhal.")
