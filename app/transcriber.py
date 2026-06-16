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


class Transcriber:
    """Vlastní faster-whisper model + jedno pracovní vlákno nad frontou."""

    def __init__(
        self,
        cfg: "AppConfig",
        on_segments: Callable[[list[tuple[float, float, str]]], None],
    ):
        self._cfg = cfg
        self._on_segments = on_segments
        self._queue: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._drain = True
        self._thread: threading.Thread | None = None
        self._model = None

    # ------------------------------------------------------------------ API

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def submit(self, samples: "np.ndarray", offset_s: float) -> None:
        self._queue.put((samples, offset_s))

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._drain = True
        self._thread = threading.Thread(
            target=self._worker, name="transcriber", daemon=True
        )
        self._thread.start()

    def stop(self, drain: bool = True) -> None:
        """Zastaví pracovní vlákno. Při ``drain=True`` nejprve zpracuje
        všechny zbývající položky ve frontě. Join s timeoutem 120 s."""
        self._drain = drain
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=120.0)
            self._thread = None

    # --------------------------------------------------------------- worker

    def _get_model(self):
        if self._model is None:
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
