"""Centralizované uložení a načítání Whisper modelů — jeden zdroj pravdy.

Každý model leží ve VLASTNÍ složce jako REÁLNÉ soubory: ``models/<name>/``
(model.bin, config.json, tokenizer.json, vocabulary.*, …). Stahujeme přes
``faster_whisper.download_model(output_dir=...)``, které píše reálné soubory
(``local_dir`` režim — žádné ``blobs/`` ani symlinky), a ``WhisperModel`` dostane
PŘÍMO cestu ke složce, takže HF cache vůbec neřeší.

PROČ takhle (W2): zabalený (PyInstaller) build CTranslate2 neumí otevřít
SYMLINKOVANÝ ``model.bin`` z výchozí HuggingFace cache
(``models/models--org--repo/snapshots/<rev>/model.bin``) — i když je soubor
přítomný a kompletní a dev build i Windows ho otevřou. Reálné soubory ve
``models/<name>/`` ten problém ruší od základu, nezdvojují místo (žádné blobs/)
a jsou triviálně otevíratelné.

Bezpečnostní zásady (z revize):
- ``is_ready`` = kompletní sada souborů + ``model.bin`` reálný soubor rozumné
  velikosti (ne „složka není prázdná").
- Migrace ze staré cache je transakční (kopie do temp + ověření velikosti +
  atomický ``os.replace``); starou cache NEMAŽE (žádná ztráta jediné kopie).
- Stahuje se jen když model CHYBÍ (žádný auto-update při startu); jeden zámek
  na model brání souběžnému stahování z více vláken.
- Aktualizace jen ručně přes ``check_for_updates`` (stáhne do temp, projeví se
  až po restartu — nikdy nepřepisuje právě načtený model).

Modul je bezpečný k importu kdekoli (faster_whisper se importuje líně; na
Linuxu/testech je mockovaný).
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time

log = logging.getLogger(__name__)

#: Kořen pro modely (relativní k pracovnímu adresáři appky = kořen buildu).
MODELS_ROOT = "models"

#: Krátké názvy -> repo na HF. Slouží k nalezení STARÉ cache při migraci a jako
#: fallback heuristika; pro stahování stačí krátký název (faster-whisper si repo
#: přeloží sám). Neznámý název -> Systran/faster-whisper-<jmeno>.
_MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "tiny.en": "Systran/faster-whisper-tiny.en",
    "base": "Systran/faster-whisper-base",
    "base.en": "Systran/faster-whisper-base.en",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v1": "Systran/faster-whisper-large-v1",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

#: Stahování přes flaky síť/antivir občas spadne (WinError 10054) — pár opakování.
MAX_DOWNLOAD_ATTEMPTS = 4
#: Prodlevy mezi pokusy (s). Poslední hodnota se opakuje, kdyby pokusů bylo víc.
RETRY_BACKOFF_S = (3, 8, 20)

#: Minimální rozumná velikost model.bin — rozliší kompletní model od útržku.
#: Nejmenší model (tiny) má ~75 MB, takže 30 MB je bezpečná spodní hranice.
_MIN_MODEL_BIN_BYTES = 30_000_000

#: Přípona dočasné složky pro stažené aktualizace (aplikují se až po restartu).
_PENDING_SUFFIX = ".update"

#: Per-model zámky (single-flight) + jejich guard, ať dvě vlákna nestahují naráz.
_locks_guard = threading.Lock()
_locks: "dict[str, threading.Lock]" = {}


def _root() -> str:
    """Absolutní cesta ke kořeni modelů (cwd je v běhu appky stabilní, ale
    abspath ruší závislost na relativní cestě i pro CTranslate2)."""
    return os.path.abspath(MODELS_ROOT)


def _safe(name: str) -> str:
    """Bezpečný název složky modelu (povolí i 'org/repo' formu)."""
    return name.replace("/", "__").strip()


def model_dir(name: str) -> str:
    """Absolutní cesta ke složce modelu: ``<root>/<name>``."""
    return os.path.join(_root(), _safe(name))


def _legacy_snapshot_dir(name: str) -> "str | None":
    """Najde snapshot ve STARÉ HF cache (``models/models--org--repo/snapshots/*``),
    který má ``model.bin``. Vrací cestu k snapshotu, nebo None."""
    repo = _MODEL_REPOS.get(name, name)
    if "/" in repo:
        org, _, rname = repo.partition("/")
    else:
        org, rname = "Systran", f"faster-whisper-{repo}"
    snapshots = os.path.join(_root(), f"models--{org}--{rname}", "snapshots")
    try:
        if not os.path.isdir(snapshots):
            return None
        for entry in sorted(os.listdir(snapshots)):
            snap = os.path.join(snapshots, entry)
            if os.path.isdir(snap) and os.path.exists(os.path.join(snap, "model.bin")):
                return snap
    except OSError:
        return None
    return None


def is_ready(name: str) -> bool:
    """Je model připravený k načtení? = ``<dir>/model.bin`` je REÁLNÝ soubor
    rozumné velikosti (ne symlink) A jsou tu i podpůrné soubory (config.json,
    tokenizer.json a nějaký vocabulary.*). Tohle je přesně to, co CTranslate2
    potřebuje — na rozdíl od „složka není prázdná" (W2)."""
    if not name:
        return True  # bez modelu se nic neřeší
    if os.path.isdir(name) or os.path.isfile(name):
        return True  # uživatel zadal přímo cestu k modelu
    d = model_dir(name)
    mb = os.path.join(d, "model.bin")
    try:
        if os.path.islink(mb) or not os.path.isfile(mb):
            return False
        if os.path.getsize(mb) < _MIN_MODEL_BIN_BYTES:
            return False
        if not os.path.isfile(os.path.join(d, "config.json")):
            return False
        if not os.path.isfile(os.path.join(d, "tokenizer.json")):
            return False
        # vocabulary bývá .txt (Systran) nebo .json (turbo) — stačí jeden
        if not (
            os.path.isfile(os.path.join(d, "vocabulary.txt"))
            or os.path.isfile(os.path.join(d, "vocabulary.json"))
        ):
            return False
        return True
    except OSError:
        return False


def _lock_for(name: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(name)
        if lk is None:
            lk = threading.Lock()
            _locks[name] = lk
        return lk


def _copy_verified(src: str, dst_dir: str, fn: str) -> bool:
    """Zkopíruje jeden soubor transakčně: dereferencuj symlink -> temp -> ověř
    velikost -> atomický replace. Vrací True při úspěchu."""
    real = os.path.realpath(src)
    if not os.path.isfile(real):
        return False
    size = os.path.getsize(real)
    tmp = os.path.join(dst_dir, fn + ".__tmp")
    try:
        shutil.copyfile(real, tmp)
        if os.path.getsize(tmp) != size:
            os.remove(tmp)
            return False
        os.replace(tmp, os.path.join(dst_dir, fn))
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _migrate_legacy(name: str) -> bool:
    """Přenese model ze STARÉ HF cache (symlink/real) do ``models/<name>`` jako
    reálné soubory, BEZ sítě a BEZ mazání staré cache. Transakční po souborech
    (kopie + ověření velikosti + atomický replace). Vrací True, když je po
    migraci model ``is_ready``."""
    snap = _legacy_snapshot_dir(name)
    if snap is None:
        return False
    dest = model_dir(name)
    try:
        os.makedirs(dest, exist_ok=True)
        for fn in os.listdir(snap):
            src = os.path.join(snap, fn)
            if os.path.isfile(src) or os.path.islink(src):
                _copy_verified(src, dest, fn)
    except OSError:
        log.warning("Migrace staré cache modelu '%s' selhala.", name, exc_info=True)
        return False
    if is_ready(name):
        log.info("Model '%s' migrován ze staré cache do '%s' (bez stahování).", name, dest)
        return True
    return False


def _download(name: str, dest: str) -> bool:
    """Jeden pokus o stažení modelu do ``dest`` jako reálné soubory."""
    from faster_whisper import download_model  # líný import (v testech mock)

    download_model(name, output_dir=dest)
    return os.path.isfile(os.path.join(dest, "model.bin"))


def _download_with_retry(name: str) -> bool:
    """Stáhne model do ``models/<name>`` s pár opakováními (flaky síť, 10054)."""
    dest = model_dir(name)
    for attempt in range(1, MAX_DOWNLOAD_ATTEMPTS + 1):
        try:
            log.info("Stahuji model '%s' do '%s' (pokus %d/%d)…", name, dest, attempt, MAX_DOWNLOAD_ATTEMPTS)
            _download(name, dest)
            if is_ready(name):
                log.info("Model '%s' stažen.", name)
                return True
            log.warning("Model '%s' po stažení není kompletní.", name)
        except Exception:  # noqa: BLE001 — stahování nesmí shodit appku
            if attempt < MAX_DOWNLOAD_ATTEMPTS:
                wait = RETRY_BACKOFF_S[min(attempt - 1, len(RETRY_BACKOFF_S) - 1)]
                log.warning(
                    "Stažení modelu '%s' selhalo (pokus %d/%d) — zkusím za %d s.",
                    name, attempt, MAX_DOWNLOAD_ATTEMPTS, wait, exc_info=True,
                )
                time.sleep(wait)
            else:
                log.warning(
                    "Stažení modelu '%s' selhalo i po %d pokusech.",
                    name, MAX_DOWNLOAD_ATTEMPTS, exc_info=True,
                )
    return is_ready(name)


def ensure_model(name: str) -> bool:
    """Zajistí, že je model k dispozici jako reálné soubory ve ``models/<name>``.

    Pořadí: hotovo? -> migrace ze staré cache (offline, bez mazání) -> stažení
    (jen když chybí, s retry). Single-flight zámek brání souběhu z více vláken.
    Vrací True/False (k dispozici/není)."""
    if not name or is_ready(name):
        return True
    with _lock_for(name):
        if is_ready(name):
            return True
        if _migrate_legacy(name):
            return True
        return _download_with_retry(name)


def load_whisper(name: str, **kwargs):
    """Postaví ``WhisperModel`` z ``models/<name>`` (cesta ke složce -> bez HF
    cache). Předtím zajistí, že je model k dispozici (``ensure_model``)."""
    from faster_whisper import WhisperModel  # líný import

    if os.path.isdir(name) or os.path.isfile(name):
        return WhisperModel(name, **kwargs)
    ensure_model(name)
    return WhisperModel(model_dir(name), **kwargs)


def apply_pending_updates(names: "tuple[str, ...]") -> int:
    """Na STARTU (před načtením jakéhokoli modelu) nasadí stažené aktualizace:
    ``models/<name>.update`` -> ``models/<name>`` atomicky. Bezpečné, protože
    v tu chvíli žádný model není mmapnutý. Vrací počet nasazených."""
    n = 0
    for name in names:
        pend = model_dir(name) + _PENDING_SUFFIX
        if not os.path.isdir(pend):
            continue
        try:
            # ověř, že pending je kompletní (přechodně přepni model_dir výpočet? ne —
            # zkontrolujeme přímo soubory v pend)
            mb = os.path.join(pend, "model.bin")
            if (
                os.path.isfile(mb)
                and not os.path.islink(mb)
                and os.path.getsize(mb) >= _MIN_MODEL_BIN_BYTES
            ):
                dest = model_dir(name)
                bak = dest + ".old"
                if os.path.isdir(dest):
                    if os.path.isdir(bak):
                        shutil.rmtree(bak, ignore_errors=True)
                    os.replace(dest, bak)
                os.replace(pend, dest)
                shutil.rmtree(bak, ignore_errors=True)
                n += 1
                log.info("Nasazena aktualizace modelu '%s'.", name)
            else:
                shutil.rmtree(pend, ignore_errors=True)  # nekompletní -> zahoď
        except OSError:
            log.warning("Nasazení aktualizace modelu '%s' selhalo.", name, exc_info=True)
    if n:
        log.info("Nasazeno %d aktualizací modelů.", n)
    return n


def check_for_updates(name: str) -> str:
    """RUČNÍ kontrola aktualizace jednoho modelu. Stáhne aktuální verzi do
    ``models/<name>.update`` (temp); pokud je kompletní, nechá ji jako pending
    a aplikuje se až při příštím startu (nikdy nepřepisuje právě načtený model).

    Vrací krátký stav: ``ready`` (k dispozici po restartu) / ``offline`` /
    ``current`` (nepodařilo se získat nic nového) / ``error``."""
    pend = model_dir(name) + _PENDING_SUFFIX
    try:
        shutil.rmtree(pend, ignore_errors=True)
        os.makedirs(pend, exist_ok=True)
        from faster_whisper import download_model  # líný import

        download_model(name, output_dir=pend)
        mb = os.path.join(pend, "model.bin")
        if os.path.isfile(mb) and not os.path.islink(mb) and os.path.getsize(mb) >= _MIN_MODEL_BIN_BYTES:
            # Když je to bajtově shodné s aktuálním modelem, nic nového -> zahoď.
            cur = os.path.join(model_dir(name), "model.bin")
            if os.path.isfile(cur) and os.path.getsize(cur) == os.path.getsize(mb):
                shutil.rmtree(pend, ignore_errors=True)
                return "current"
            log.info("Aktualizace modelu '%s' stažena (projeví se po restartu).", name)
            return "ready"
        shutil.rmtree(pend, ignore_errors=True)
        return "current"
    except Exception:  # noqa: BLE001
        shutil.rmtree(pend, ignore_errors=True)
        log.warning("Kontrola aktualizace modelu '%s' selhala (síť?).", name, exc_info=True)
        return "offline"
