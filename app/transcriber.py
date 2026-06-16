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


class Transcriber:
    """Vlastní faster-whisper model + jedno pracovní vlákno nad frontou."""

    def __init__(
        self,
        cfg: "AppConfig",
        on_segments: Callable[[list[tuple[float, float, str]]], None],
        on_error: "Callable[[str], None] | None" = None,
        model_factory: "Callable[[], object] | None" = None,
    ):
        self._cfg = cfg
        self._on_segments = on_segments
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

    def _get_model(self):
        if self._model is None:
            if self._model_factory is not None:
                self._model = self._model_factory()
                return self._model
            from faster_whisper import WhisperModel  # líný import

            import os

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
        model = self._get_model()
        # "auto" / "" -> autodetekce jazyka (každý blok zvlášť — zvládne
        # i střídání češtiny a angličtiny mezi meetingy či v rámci jednoho).
        lang = self._cfg.language if self._cfg.language not in ("", "auto") else None
        segments, _info = model.transcribe(
            samples,
            language=lang,
            multilingual=lang is None,  # střídání jazyků i uvnitř 20s bloku
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
            initial_prompt="Přepis českého pracovního meetingu." if lang == "cs" else None,
        )
        out: list[tuple[float, float, str]] = []
        for seg in segments:
            text = (seg.text or "").strip()
            if not text:
                continue
            out.append((offset_s + seg.start, offset_s + seg.end, text))
        if out:
            self._on_segments(out)
