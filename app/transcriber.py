"""Přepis zvuku přes faster-whisper.

``faster_whisper`` se importuje LÍNĚ až uvnitř pracovního vlákna (při prvním
použití modelu), takže import tohoto modulu je bezpečný kdekoli — včetně
Linuxu/testů, kde je knihovna nahrazena mockem v ``tests/conftest.py``.

Model: ``WhisperModel(cfg.live_model, device='cpu', compute_type='int8',
download_root='models')``. Pracovní vlákno konzumuje ``queue.Queue`` dvojic
``(samples, offset_s)`` a volá ``on_segments(list[(t0, t1, text)])``.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover - jen pro typy
    import numpy as np

    from app.config import AppConfig

log = logging.getLogger(__name__)

#: Strop fronty živého přepisu. Živý přepis je best-effort (finální vzniká
#: z WAV), takže při zahlcení (slabé CPU, dlouhý hovor) raději zahazujeme
#: nejstarší bloky, než aby paměť rostla bez limitu (H1). ~30 bloků = ~10 min
#: zpoždění při 20s blocích, než začneme zahazovat.
MAX_QUEUE_CHUNKS = 30
#: Drain timeout při stop() — výrazně pod původních 120 s (H1).
DRAIN_TIMEOUT_S = 30.0

#: Krátké názvy modelů faster-whisper -> repo na HuggingFace. Stačí položky,
#: které appka reálně používá (live_model/post_model); pro neznámý název
#: padáme na heuristiku níž, takže tabulka nemusí být úplná (M9).
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


def model_is_downloaded(model_name: str, download_root: str = "models") -> bool:
    """Heuristika (M9): je model ``model_name`` už stažený v ``download_root``?

    Když ne, je jisté, že nejbližší stavění modelu spustí ~GB stahování a UI to
    má ohlásit ("Stahuji model…"), aby appka nevypadala zamrzlá. Detekce je
    schválně konzervativní: při jakékoli nejistotě vrací ``True`` (= nehlásit
    stahování), aby se zbytečně neukazoval indikátor u modelu, který je ve
    skutečnosti k dispozici.

    Pravidla:
    - lokální cesta (existující adresář / soubor) -> považujeme za stažený;
    - jinak název přeložíme na repo ``org/jmeno`` (tabulka, jinak heuristika
      Systran/faster-whisper-<jmeno>) a hledáme cache HuggingFace
      ``download_root/models--<org>--<jmeno>/snapshots/*/`` s nějakým obsahem.
    """
    if not model_name:
        return True  # bez modelu se nic nestahuje
    # Uživatel může zadat přímo cestu k lokálnímu modelu — pak se nestahuje.
    if os.path.isdir(model_name) or os.path.isfile(model_name):
        return True

    repo = _MODEL_REPOS.get(model_name, model_name)
    if "/" in repo:
        org, _, name = repo.partition("/")
    else:
        # Neznámý krátký název: faster-whisper publikuje pod Systran/.
        org, name = "Systran", f"faster-whisper-{repo}"
    cache_name = f"models--{org}--{name}"
    snapshots = os.path.join(download_root, cache_name, "snapshots")
    try:
        if not os.path.isdir(snapshots):
            return False
        # Stažený model má aspoň jeden snapshot s obsahem (model.bin apod.).
        for entry in os.listdir(snapshots):
            snap = os.path.join(snapshots, entry)
            if os.path.isdir(snap) and os.listdir(snap):
                return True
        return False
    except OSError:
        # Nejistota (chyba FS) -> konzervativně bez hlášení stahování.
        return True


class Transcriber:
    """Vlastní faster-whisper model + jedno pracovní vlákno nad frontou."""

    def __init__(
        self,
        cfg: "AppConfig",
        on_segments: Callable[[list[tuple[float, float, str]]], None],
        on_error: "Callable[[str], None] | None" = None,
        model_factory: "Callable[[], object] | None" = None,
        attendees: "list[str] | None" = None,
    ):
        self._cfg = cfg
        self._on_segments = on_segments
        #: Jména/e-maily účastníků meetingu — doplní se do initial_prompt
        #: slovníku, ať Whisper trefí jejich jména (kvalita přepisu). Prázdné
        #: u ručního záznamu nebo když je Recorder nepředá.
        self._attendees = list(attendees or [])
        #: Voláno (z vlákna start()) při neúspěšném načtení modelu — UI to má
        #: tvrdě ohlásit místo nekonečného opakování po blocích (H1).
        self._on_error = on_error or (lambda msg: None)
        self._model_factory = model_factory
        self._queue: queue.Queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
        self._stop = threading.Event()
        self._drain = True
        self._thread: threading.Thread | None = None
        self._model = None
        self._dropped = 0
        #: Stav stavění modelu pro UI (M9): "" | "downloading" | "loading" |
        #: "ready". Čte se z UI vlákna (jen čtení str atributu je bezpečné).
        self.model_status: str = ""
        #: Voláno při změně model_status (např. "downloading"). UI to musí
        #: marshalovat do svého vlákna stejně jako ostatní callbacky (L6).
        self.on_model_status: "Callable[[str], None]" = lambda state: None

    # ------------------------------------------------------------------ API

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def submit(self, samples: "np.ndarray", offset_s: float) -> None:
        """Zařadí blok k přepisu. Při plné frontě (Whisper nestíhá realtime)
        zahodí nejstarší blok a vloží nový — živý přepis je best-effort, finální
        stejně vzniká z WAV. Nikdy neblokuje zachytávací vlákno (H1)."""
        try:
            self._queue.put_nowait((samples, offset_s))
        except queue.Full:
            try:
                self._queue.get_nowait()  # zahoď nejstarší
                self._queue.task_done()
            except queue.Empty:
                pass
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 10 == 0:
                log.warning(
                    "Fronta živého přepisu plná (%d) — zahazuji nejstarší blok "
                    "(zatím zahozeno %d). Finální přepis vznikne z WAV.",
                    MAX_QUEUE_CHUNKS,
                    self._dropped,
                )
            try:
                self._queue.put_nowait((samples, offset_s))
            except queue.Full:
                pass

    def start(self) -> None:
        """Spustí pracovní vlákno. Model se postaví HNED (jednou) — když se
        nepovede (poškozené stažení, plný disk, offline), vyhodí výjimku, aby
        ji volající (Recorder/UI) tvrdě ohlásil místo tichého opakování (H1)."""
        if self._thread is not None and self._thread.is_alive():
            return
        try:
            self._get_model()
        except Exception as exc:  # noqa: BLE001
            log.exception("Načtení modelu živého přepisu selhalo.")
            self._on_error(str(exc))
            raise
        self._stop.clear()
        self._drain = True
        self._dropped = 0
        self._thread = threading.Thread(
            target=self._worker, name="transcriber", daemon=True
        )
        self._thread.start()

    def stop(self, drain: bool = True) -> None:
        """Zastaví pracovní vlákno. Při ``drain=True`` nejprve zpracuje
        zbývající položky ve frontě. Join s timeoutem ``DRAIN_TIMEOUT_S``;
        mrtvé vlákno se nejoinuje (vrátilo by se hned, ale buďme explicitní)."""
        self._drain = drain
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=DRAIN_TIMEOUT_S)

    # --------------------------------------------------------------- worker

    def _set_model_status(self, state: str) -> None:
        """Nastaví stav stavění modelu a probublá ho do UI (M9)."""
        if state == self.model_status:
            return
        self.model_status = state
        try:
            self.on_model_status(state)
        except Exception:  # noqa: BLE001 - callback UI nesmí shodit přepis
            log.exception("Callback on_model_status selhal.")

    def _get_model(self):
        if self._model is None:
            if self._model_factory is not None:
                # Testovací/injektovaný model — stav nastavíme bez detekce FS.
                self._set_model_status("loading")
                self._model = self._model_factory()
                self._set_model_status("ready")
                return self._model
            from faster_whisper import WhisperModel  # líný import

            # M9: když model ještě není v models/, nejbližší build spustí
            # ~GB stahování — ohlásíme to UI předem, ať nevypadá zamrzlé.
            downloaded = model_is_downloaded(self._cfg.live_model, "models")
            self._set_model_status("loading" if downloaded else "downloading")

            # Omezíme počet CPU vláken Whisperu, aby zachytávací vlákna
            # (WASAPI) nehladověla a nevznikaly výpadky "data discontinuity".
            cpu_threads = max(2, (os.cpu_count() or 8) // 2)
            self._model = WhisperModel(
                self._cfg.live_model,
                device="cpu",
                compute_type="int8",
                download_root="models",
                cpu_threads=cpu_threads,
                num_workers=1,
            )
            self._set_model_status("ready")
        return self._model

    def _worker(self) -> None:
        while True:
            if self._stop.is_set():
                if not self._drain or self._queue.empty():
                    break
            try:
                samples, offset_s = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._process(samples, offset_s)
            except Exception:  # noqa: BLE001 - chyba jednoho bloku nesmí zabít vlákno
                log.exception("Přepis bloku (offset %.1f s) selhal — pokračuji.", offset_s)
            finally:
                self._queue.task_done()

    def _process(self, samples: "np.ndarray", offset_s: float) -> None:
        from app.glossary import build_initial_prompt

        model = self._get_model()
        # Jazyk detekujeme JEDNOU (multilingual=False): konkrétní kód z configu
        # předáme natvrdo, "auto"/"" -> language=None (faster-whisper detekuje
        # z bloku a dál ho nepřepíná po segmentech — to dřív tříštilo segmenty
        # i kazilo přesnost).
        lang = self._cfg.language if self._cfg.language not in ("", "auto") else None
        # initial_prompt (slovník jmen/termínů) dáváme pro JAKÝKOLI jazyk —
        # zlepší přepis názvů (elem6, Claude, …) i účastníků meetingu.
        segments, _info = model.transcribe(
            samples,
            language=lang,
            multilingual=False,  # jeden jazyk pro celý blok (žádná re-detekce)
            vad_filter=True,
            vad_parameters=dict(min_speech_duration_ms=250, max_speech_duration_s=30),
            beam_size=1,
            condition_on_previous_text=False,
            initial_prompt=build_initial_prompt(self._attendees),
        )
        out: list[tuple[float, float, str]] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append((offset_s + seg.start, offset_s + seg.end, text))
        if out:
            self._on_segments(out)
