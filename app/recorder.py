"""Orchestrace nahrávání: capture -> transcriber -> storage.

Stavový automat s injektovanými závislostmi (capture_factory,
transcriber_factory, note_store) — plně testovatelný s fake objekty.
Stavové přechody jsou chráněny zámkem (``threading.Lock``).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

from app.models import Meeting, Platform, RecorderState

if TYPE_CHECKING:  # pragma: no cover - jen pro typy
    import numpy as np

    from app.config import AppConfig
    from app.storage import NoteStore

log = logging.getLogger(__name__)


def _default_capture_factory(cfg, on_chunk):
    from app.audio_capture import AudioCapture

    return AudioCapture(cfg, on_chunk)


def _default_transcriber_factory(cfg, on_segments, attendees=None, title=None):
    from app.transcriber import Transcriber

    return Transcriber(cfg, on_segments, attendees=attendees, title=title)


class Recorder:
    """Stavový automat nahrávání.

    start(meeting): vytvoří poznámku, spustí transcriber + capture,
    state=RECORDING. on_chunk -> transcriber.submit; on_segments ->
    store.append_segment + on_segment callbacky. stop(): capture.stop,
    transcriber.stop(drain=True), store.finalize, state=IDLE.
    """

    def __init__(
        self,
        cfg: "AppConfig",
        note_store: "NoteStore",
        capture_factory: Callable | None = None,
        transcriber_factory: Callable | None = None,
    ):
        self._cfg = cfg
        self._store = note_store
        self._capture_factory = capture_factory or _default_capture_factory
        self._transcriber_factory = transcriber_factory or _default_transcriber_factory

        self._lock = threading.Lock()
        self.state: RecorderState = RecorderState.IDLE
        self.current_meeting: Meeting | None = None
        self.note_path: str | None = None

        # POZOR: tyto callback listy se volají z PRACOVNÍCH vláken
        # (transcriber/capture/stop). UI je proto musí marshalovat do svého
        # vlákna (viz _Bridge v main_window). Nikdy do nich nepřipojuj přímý
        # dotek widgetu — vznikl by cross-thread bug (L6).
        self.on_state_changed: list[Callable[[RecorderState], None]] = []
        self.on_segment: list[Callable[[float, float, str], None]] = []
        #: Voláno po dokončení záznamu: cb(note_path, wav_path)
        self.on_finished: list[Callable[[str, str], None]] = []
        #: Voláno z capture vlákna při výpadku zařízení uprostřed záznamu (H5):
        #: cb(zpráva). UI to marshaluje a zastaví záznam + upozorní uživatele.
        self.on_device_error: list[Callable[[str], None]] = []

        self._capture = None
        self._transcriber = None
        self._started_at: float | None = None
        self._started_wall: str = ""  # ISO čas startu (L9: deklarováno v __init__)
        self._wav = None
        self._wav_lock = threading.Lock()
        self.wav_path: str | None = None

    # ------------------------------------------------------------------ API

    @property
    def elapsed_s(self) -> float:
        """Sekundy od začátku probíhajícího nahrávání (0.0 mimo nahrávání).

        Stav i ``_started_at`` čteme jako konzistentní snapshot pod zámkem (L1)
        — jinak mohl stop souběžně přepsat jedno z polí a vrátit 0.0/zastaralou
        hodnotu."""
        with self._lock:
            started = self._started_at
            state = self.state
        if started is None or state not in (
            RecorderState.RECORDING,
            RecorderState.FINALIZING,
        ):
            return 0.0
        return time.monotonic() - started

    def start(self, meeting: Meeting) -> None:
        with self._lock:
            if self.state not in (RecorderState.IDLE, RecorderState.ARMED):
                raise RuntimeError(
                    f"Nahrávání nelze spustit ve stavu '{self.state.value}'."
                )

            path = self._store.create_note(meeting)
            # Účastníky + název meetingu předáme transcriberu pro initial_prompt
            # slovník (lepší přepis jmen). Preferujeme zobrazovaná jména (CN z
            # kalendáře); když chybí, padáme na e-maily. Fake factories v testech
            # berou jen 2 argumenty — pak je vynecháme (zpětná kompatibilita).
            prompt_names = (
                getattr(meeting, "attendee_names", None) or meeting.attendees
            )
            try:
                transcriber = self._transcriber_factory(
                    self._cfg,
                    self._handle_segments,
                    attendees=prompt_names,
                    title=meeting.title,
                )
            except TypeError:
                transcriber = self._transcriber_factory(self._cfg, self._handle_segments)
            capture = self._capture_factory(self._cfg, self._handle_chunk)
            # Probublání výpadku zařízení uprostřed hovoru (H5), pokud to
            # capture umí (AudioCapture ano; fake objekty v testech nemusí).
            if hasattr(capture, "on_device_error"):
                capture.on_device_error = self._handle_device_error

            # Transcriber se spustí jako první: při selhání načtení modelu (H1)
            # vyhodí výjimku, kterou volající (plánovač/UI) tvrdě ohlásí.
            transcriber.start()
            try:
                capture.start()
            except Exception:
                try:
                    transcriber.stop(drain=False)
                except Exception:  # noqa: BLE001
                    log.exception("Úklid transcriberu po chybě zařízení selhal.")
                raise

            self._capture = capture
            self._transcriber = transcriber
            self.current_meeting = meeting
            self.note_path = path
            self.wav_path = self._open_wav(path)
            self._started_at = time.monotonic()
            self._started_wall = datetime.now().astimezone().isoformat(timespec="seconds")
            self.state = RecorderState.RECORDING

        self._notify_state(RecorderState.RECORDING)

    def start_manual(self, title: str = "Ruční záznam") -> None:
        """Spustí ručně zahájené nahrávání se syntetickou schůzkou (2 h)."""
        now = datetime.now().astimezone()
        meeting = Meeting(
            uid=f"manual:{now.isoformat()}",
            title=title,
            start=now,
            end=now + timedelta(hours=2),
            platform=Platform.OTHER,
        )
        self.start(meeting)

    def stop(self) -> None:
        with self._lock:
            if self.state is not RecorderState.RECORDING:
                return
            self.state = RecorderState.FINALIZING
            capture = self._capture
            transcriber = self._transcriber
            path = self.note_path
            meeting = self.current_meeting
            started_wall = getattr(self, "_started_wall", "")

        self._notify_state(RecorderState.FINALIZING)

        duration_s = 0.0
        if capture is not None:
            try:
                duration_s = capture.stop()
            except Exception:  # noqa: BLE001
                log.exception("Zastavení zachytávání zvuku selhalo.")
        if transcriber is not None:
            try:
                transcriber.stop(drain=True)
            except Exception:  # noqa: BLE001
                log.exception("Zastavení transcriberu selhalo.")
        with self._wav_lock:
            if self._wav is not None:
                try:
                    self._wav.close()
                except Exception:  # noqa: BLE001
                    log.exception("Zavření WAV souboru selhalo.")
                self._wav = None
        wav_path = self.wav_path

        if path is not None:
            self._store.finalize(path, duration_s)
            if meeting is not None:
                try:
                    self._store.index_add(meeting, path, duration_s, started_wall)
                except Exception:  # noqa: BLE001 - index je bonus, nesmí blokovat
                    log.exception("Zápis do index.jsonl selhal.")

        with self._lock:
            self._capture = None
            self._transcriber = None
            self.current_meeting = None
            self.note_path = None
            self.wav_path = None
            self._started_at = None
            self.state = RecorderState.IDLE

        self._notify_state(RecorderState.IDLE)

        if path is not None and wav_path is not None:
            for cb in list(self.on_finished):
                try:
                    cb(path, wav_path)
                except Exception:  # noqa: BLE001
                    log.exception("Callback on_finished selhal.")

    # ---------------------------------------------------------- callbacky

    def _open_wav(self, note_path: str):
        """Otevře WAV soubor (16 kHz STEREO 16bit: L = mikrofon/Ivan,
        R = loopback/ostatní) vedle poznámky. Oddělené kanály slouží
        post-processoru k atribuci mluvčích. Při selhání jen loguje
        (WAV je bonus pro post-přepis, nesmí zablokovat nahrávání)."""
        import wave

        wav_path = note_path[:-3] + ".wav" if note_path.endswith(".md") else note_path + ".wav"
        try:
            w = wave.open(wav_path, "wb")
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(self._cfg.sample_rate)
            self._wav = w
            return wav_path
        except Exception:  # noqa: BLE001
            log.exception("Nepodařilo se otevřít WAV soubor pro záznam.")
            self._wav = None
            return None

    def _handle_chunk(self, samples: "np.ndarray", offset_s: float) -> None:
        import numpy as np

        arr = np.asarray(samples, dtype=np.float32)
        stereo = np.stack([arr, arr], axis=1) if arr.ndim == 1 else arr

        with self._wav_lock:
            if self._wav is not None:
                try:
                    pcm = (np.clip(stereo, -1.0, 1.0) * 32767.0).astype(np.int16)
                    self._wav.writeframes(pcm.tobytes())  # interleaved L/R
                except Exception:  # noqa: BLE001
                    log.exception("Zápis do WAV selhal — vypínám ukládání WAV.")
                    self._wav = None

        transcriber = self._transcriber
        if transcriber is not None:
            # živý přepis dostává mono mix (Whisper kanály nepotřebuje)
            mono = np.clip(stereo.mean(axis=1), -1.0, 1.0).astype(np.float32)
            transcriber.submit(mono, offset_s)

    def _handle_segments(self, segments: list[tuple[float, float, str]]) -> None:
        path = self.note_path
        for t0, t1, text in segments:
            if path is not None:
                try:
                    self._store.append_segment(path, t0, t1, text)
                except Exception:  # noqa: BLE001
                    log.exception("Zápis segmentu do poznámky selhal.")
            for cb in list(self.on_segment):
                try:
                    cb(t0, t1, text)
                except Exception:  # noqa: BLE001
                    log.exception("Callback on_segment selhal.")

    def _handle_device_error(self, message: str) -> None:
        """Voláno z capture vlákna při výpadku zařízení uprostřed záznamu (H5).
        Jen probublá zprávu do odběratelů (UI) — samotné zastavení musí
        proběhnout jinde (UI vlákno přes request_stop), ne z capture vlákna."""
        log.warning("Výpadek zvukového zařízení během záznamu: %s", message)
        for cb in list(self.on_device_error):
            try:
                cb(message)
            except Exception:  # noqa: BLE001
                log.exception("Callback on_device_error selhal.")

    def _notify_state(self, state: RecorderState) -> None:
        for cb in list(self.on_state_changed):
            try:
                cb(state)
            except Exception:  # noqa: BLE001
                log.exception("Callback on_state_changed selhal.")
