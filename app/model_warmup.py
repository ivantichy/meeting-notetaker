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

Plně defenzivní: jakákoliv chyba (offline, výpadek HF, plný disk) se jen
zaloguje a NIKDY neshodí appku. Nahrávání má svůj dosavadní lazy fallback, takže
i kdyby předstažení selhalo, chování zůstává jako dřív (jen s rizikem, že první
hovor dál čeká na stažení).
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

from app.transcriber import model_is_downloaded

if TYPE_CHECKING:  # pragma: no cover - jen pro typy
    from app.config import AppConfig

log = logging.getLogger(__name__)

#: Stejný kořen cache jako ``download_root`` v transcriber/post_processor.
#: Relativní k pracovnímu adresáři appky (main.py dělá chdir do kořene buildu),
#: takže předstažení míří do TÉŽE složky, ze které pak nahrávání model čte.
DOWNLOAD_ROOT = "models"


def _ensure_downloaded(model_name: str) -> None:
    """Idempotentně zajistí stažení jednoho modelu do ``DOWNLOAD_ROOT``.

    Když už je v cache, nesahá na síť (rychlý lokální test ``model_is_downloaded``).
    Jinak stáhne jen soubory přes ``faster_whisper.download_model`` se stejným
    ``cache_dir``, jaký používá ``WhisperModel(..., download_root="models")``
    interně — tedy bez načtení modelu do paměti.
    """
    if not model_name:
        return
    if model_is_downloaded(model_name, DOWNLOAD_ROOT):
        log.info("Model '%s' je už stažený — předstažení přeskočeno.", model_name)
        return
    from faster_whisper import download_model  # líný import (v testech mock)

    log.info("Předstahuji model '%s' do '%s/'…", model_name, DOWNLOAD_ROOT)
    download_model(model_name, cache_dir=DOWNLOAD_ROOT)
    log.info("Model '%s' předstažen.", model_name)


def _warmup(model_names: "tuple[str, ...]") -> None:
    """Projde modely a každý se pokusí stáhnout; chyba jednoho nezastaví druhý."""
    for name in model_names:
        try:
            _ensure_downloaded(name)
        except Exception:  # noqa: BLE001 — předstažení nesmí nikdy shodit appku
            log.warning(
                "Předstažení modelu '%s' selhalo — nahrávání ho zkusí stáhnout "
                "lazy jako dřív.",
                name,
                exc_info=True,
            )


def start_model_warmup(cfg: "AppConfig") -> threading.Thread:
    """Na pozadí předstáhne ``live_model`` i ``post_model`` z configu (W1).

    Neblokuje start UI (daemon vlákno). Zachová pořadí (živý nejdřív — je
    potřeba dřív), ale vynechá prázdné hodnoty i duplicitu (když ``live_model``
    == ``post_model``, stahuje se jen jednou). Vrací spuštěné vlákno (kvůli
    testovatelnosti a případnému join při ukončení).
    """
    names: "tuple[str, ...]" = tuple(
        dict.fromkeys(n for n in (cfg.live_model, cfg.post_model) if n)
    )
    if names:
        log.info("Spouštím předstažení modelů na pozadí: %s", ", ".join(names))
    thread = threading.Thread(
        target=_warmup, args=(names,), name="model-warmup", daemon=True
    )
    thread.start()
    return thread
