"""Předstažení Whisper modelů na pozadí při startu (W1).

Problém (proč to existuje): faster-whisper stahuje model až ve chvíli, kdy ho
poprvé potřebuje — tedy při startu nahrávání PRVNÍHO hovoru. U čerstvé
instalace (prázdná cache ``models/``) to znamená stáhnout ~0,5 GB (živý
``small``) nebo ~1,6 GB (finální ``large-v3-turbo``) přesně v okamžiku, kdy má
začít přepis. CTranslate2 pak otevře ještě nedostažený ``model.bin`` a záznam
spadne („Unable to open file 'model.bin'"). První hovor po instalaci tak přijde
vniveč.

Řešení: hned po startu appky na pozadí (daemon vlákno) zajistíme, že jsou oba
modely z configu (``live_model`` i ``post_model``) stažené do TÉŽE cache, kterou
appka používá při nahrávání (``download_root="models"``). Když chybí, stáhnou
se; když jsou, neděláme nic. Stahujeme jen SOUBORY (``faster_whisper.
download_model`` — stejná cesta, jakou ``WhisperModel(..., download_root=...)``
volá interně), model se NEnačítá do RAM, takže start nezdraží paměťově.

Stav stahování je pozorovatelný (``ModelWarmup.downloading`` / ``finished``),
aby UI mohlo ukázat indikátor a hlavně aby se plánovač NEPOKOUŠEL spustit
záznam, dokud živý model není stažený (jinak by to spadlo). Plně defenzivní:
jakákoliv chyba se jen zaloguje a NIKDY neshodí appku.
"""
from __future__ import annotations

import logging
import threading

from app.transcriber import model_is_downloaded

log = logging.getLogger(__name__)

#: Stejný kořen cache jako ``download_root`` v transcriber/post_processor.
#: Relativní k pracovnímu adresáři appky (main.py dělá chdir do kořene buildu),
#: takže předstažení míří do TÉŽE složky, ze které pak nahrávání model čte.
DOWNLOAD_ROOT = "models"


class ModelWarmup:
    """Handle na běžící předstažení modelů + jeho pozorovatelný stav.

    Stav čte UI vlákno (``downloading`` / ``finished``), zapisuje pracovní
    vlákno — vše pod zámkem, takže čtení napříč vlákny je bezpečné.
    """

    def __init__(self, model_names: "tuple[str, ...]") -> None:
        self._lock = threading.Lock()
        self._names = model_names
        self._downloading: "str | None" = None
        self._finished = False
        #: Vyplní ``start_model_warmup`` po spuštění vlákna.
        self.thread: "threading.Thread | None" = None

    # ------------------------------------------------------- pracovní vlákno
    def _run(self) -> None:
        for name in self._names:
            try:
                if model_is_downloaded(name, DOWNLOAD_ROOT):
                    log.info("Model '%s' je už stažený — předstažení přeskočeno.", name)
                    continue
                self._set_downloading(name)
                from faster_whisper import download_model  # líný import (v testech mock)

                log.info("Předstahuji model '%s' do '%s/'…", name, DOWNLOAD_ROOT)
                download_model(name, cache_dir=DOWNLOAD_ROOT)
                log.info("Model '%s' předstažen.", name)
            except Exception:  # noqa: BLE001 — předstažení nesmí nikdy shodit appku
                log.warning(
                    "Předstažení modelu '%s' selhalo — nahrávání ho zkusí stáhnout "
                    "lazy jako dřív.",
                    name,
                    exc_info=True,
                )
            finally:
                self._clear_downloading(name)
        self._mark_finished()

    def _set_downloading(self, name: str) -> None:
        with self._lock:
            self._downloading = name

    def _clear_downloading(self, name: str) -> None:
        with self._lock:
            if self._downloading == name:
                self._downloading = None

    def _mark_finished(self) -> None:
        with self._lock:
            self._downloading = None
            self._finished = True

    # --------------------------------------------------------- čtení (UI)
    @property
    def downloading(self) -> "str | None":
        """Název modelu, který se právě stahuje, nebo ``None``."""
        with self._lock:
            return self._downloading

    @property
    def finished(self) -> bool:
        """True, když předstažení doběhlo (všechny modely vyřízené)."""
        with self._lock:
            return self._finished

    # ------------------------------------------------- pohodlí / testy
    def join(self, timeout: "float | None" = None) -> None:
        if self.thread is not None:
            self.thread.join(timeout)

    def is_alive(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


def start_model_warmup(cfg) -> ModelWarmup:
    """Na pozadí předstáhne ``live_model`` i ``post_model`` z configu (W1).

    Neblokuje start UI (daemon vlákno). Zachová pořadí (živý nejdřív — je
    potřeba dřív), ale vynechá prázdné hodnoty i duplicitu (když ``live_model``
    == ``post_model``, stahuje se jen jednou). Vrací ``ModelWarmup`` handle, ze
    kterého UI čte stav.
    """
    names: "tuple[str, ...]" = tuple(
        dict.fromkeys(n for n in (cfg.live_model, cfg.post_model) if n)
    )
    handle = ModelWarmup(names)
    if names:
        log.info("Spouštím předstažení modelů na pozadí: %s", ", ".join(names))
    thread = threading.Thread(
        target=handle._run, name="model-warmup", daemon=True
    )
    handle.thread = thread
    thread.start()
    return handle
