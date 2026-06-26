"""Předstažení Whisper modelů na pozadí při startu (W1) — tenký běhový obal.

Veškerá logika (cesty, připravenost, migrace staré cache, stahování s retry,
nasazení stažených aktualizací) žije v ``app.model_store``. Tady jen na pozadí
(daemon vlákno) projedeme modely z configu a zveřejníme STAV pro UI:
``downloading`` (který se zrovna stahuje), ``finished`` (hotovo) a ``failed``
(které se nepodařilo získat — UI to dá najevo).

Proč warm-up vůbec: faster-whisper by jinak stahoval model až ve chvíli prvního
hovoru; u čerstvé instalace by to první call zdrželo/shodilo. Předstažení na
pozadí to vyřeší dopředu. Plně defenzivní — nikdy neshodí appku.
"""
from __future__ import annotations

import logging
import threading

from app import model_store

log = logging.getLogger(__name__)

#: Kvůli zpětné kompatibilitě (skripty/testy): kořen modelů.
DOWNLOAD_ROOT = model_store.MODELS_ROOT


class ModelWarmup:
    """Handle na běžící předstažení + jeho pozorovatelný stav (čte UI vlákno)."""

    def __init__(self, model_names: "tuple[str, ...]") -> None:
        self._lock = threading.Lock()
        self._names = model_names
        self._downloading: "str | None" = None
        self._finished = False
        self._failed: "set[str]" = set()
        self.thread: "threading.Thread | None" = None

    # ------------------------------------------------------- pracovní vlákno
    def _run(self) -> None:
        # Nejdřív nasaď případné stažené aktualizace (před načtením modelů).
        try:
            model_store.apply_pending_updates(self._names)
        except Exception:  # noqa: BLE001
            log.warning("Nasazení stažených aktualizací selhalo.", exc_info=True)
        for name in self._names:
            try:
                if model_store.is_ready(name):
                    log.info("Model '%s' je k dispozici — předstažení přeskočeno.", name)
                    continue
                self._set_downloading(name)
                ok = model_store.ensure_model(name)
            except Exception:  # noqa: BLE001 — předstažení nesmí shodit appku
                log.warning("Předstažení modelu '%s' selhalo.", name, exc_info=True)
                ok = False
            finally:
                self._clear_downloading(name)
            if not ok:
                self._mark_failed(name)
        self._mark_finished()

    def _set_downloading(self, name: str) -> None:
        with self._lock:
            self._downloading = name

    def _clear_downloading(self, name: str) -> None:
        with self._lock:
            if self._downloading == name:
                self._downloading = None

    def _mark_failed(self, name: str) -> None:
        with self._lock:
            self._failed.add(name)

    def _mark_finished(self) -> None:
        with self._lock:
            self._downloading = None
            self._finished = True

    # --------------------------------------------------------- čtení (UI)
    @property
    def downloading(self) -> "str | None":
        """Název modelu, který se právě stahuje, nebo None."""
        with self._lock:
            return self._downloading

    @property
    def finished(self) -> bool:
        """True, když předstažení doběhlo (všechny modely vyřízené)."""
        with self._lock:
            return self._finished

    @property
    def failed(self) -> "tuple[str, ...]":
        """Modely, které se nepodařilo získat (UI varuje)."""
        with self._lock:
            return tuple(self._failed)

    # ------------------------------------------------- pohodlí / testy
    def join(self, timeout: "float | None" = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


def start_model_warmup(cfg) -> ModelWarmup:
    """Na pozadí (daemon) předstáhne ``live_model`` i ``post_model`` z configu.

    Neblokuje UI, zachová pořadí (živý nejdřív), vynechá prázdné i duplicitu.
    Vrací ``ModelWarmup`` handle, ze kterého UI čte stav."""
    names: "tuple[str, ...]" = tuple(
        dict.fromkeys(n for n in (cfg.live_model, cfg.post_model) if n)
    )
    handle = ModelWarmup(names)
    if names:
        log.info("Spouštím předstažení modelů na pozadí: %s", ", ".join(names))
    thread = threading.Thread(target=handle._run, name="model-warmup", daemon=True)
    handle.thread = thread
    thread.start()
    return handle
