"""Zachytávání zvuku: WASAPI loopback (výstup reproduktorů) + mikrofon.

Používá knihovnu `soundcard`, ale importuje ji LÍNĚ (uvnitř metod), takže
import tohoto modulu je bezpečný kdekoli — včetně Linuxu/testů, kde je
`soundcard` nahrazeno mockem v `tests/conftest.py`.

Architektura:
  - dvě daemon vlákna (loopback + mikrofon), každé čte bloky přes
    ``recorder.record(numframes=...)`` na nativní vzorkovací frekvenci 48 kHz,
    float32, a ukládá je do front;
  - třetí (mixovací) vlákno páruje bloky podle pořadí a skládá je do
    STEREO pole (n, 2): kanál 0 = mikrofon (Ivan), kanál 1 = loopback
    (ostatní účastníci) — oddělené kanály slouží k atribuci mluvčích;
  - po naplnění ``cfg.chunk_seconds`` se blok převzorkuje 48000 ->
    ``cfg.sample_rate`` lineární interpolací (numpy) a předá callbacku
    ``on_chunk(chunk_stereo_f32, offset_s)``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import TYPE_CHECKING, Callable

import numpy as np

if TYPE_CHECKING:  # pragma: no cover - jen pro typy
    from app.config import AppConfig

log = logging.getLogger(__name__)

#: Nativní vzorkovací frekvence zařízení (WASAPI mixer pracuje na 48 kHz).
NATIVE_RATE = 48000
#: Velikost jednoho čteného bloku (0.5 s při 48 kHz). Větší blok = méně
#: režije Pythonu a menší riziko "data discontinuity", když Whisper vytěžuje CPU.
BLOCK_FRAMES = 24000


def _to_mono_f32(data: np.ndarray) -> np.ndarray:
    """Stereo (či vícekanálové) pole -> mono float32 průměrem kanálů."""
    arr = np.asarray(data, dtype=np.float32)
    if arr.ndim == 2:
        if arr.shape[1] > 1:
            arr = arr.mean(axis=1)
        else:
            arr = arr[:, 0]
    return np.ascontiguousarray(arr, dtype=np.float32)


def _resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Převzorkování lineární interpolací (numpy), float32."""
    if src_rate == dst_rate or len(samples) == 0:
        return samples.astype(np.float32, copy=False)
    n_dst = max(1, int(round(len(samples) * dst_rate / src_rate)))
    src_t = np.arange(len(samples), dtype=np.float64) / src_rate
    dst_t = np.arange(n_dst, dtype=np.float64) / dst_rate
    return np.interp(dst_t, src_t, samples).astype(np.float32)


class AudioCapture:
    """Zachytává default speaker loopback + default mikrofon.

    ``on_chunk(samples_mono_f32, chunk_start_offset_seconds)`` je voláno
    z mixovacího vlákna pro každý hotový blok délky ``cfg.chunk_seconds``.
    """

    def __init__(self, cfg: "AppConfig", on_chunk: Callable[[np.ndarray, float], None]):
        self._cfg = cfg
        self._on_chunk = on_chunk

        self._stop = threading.Event()
        self._buf_lock = threading.Lock()
        self._mic_q: deque[np.ndarray] = deque()
        self._loop_q: deque[np.ndarray] = deque()

        self._capture_threads: list[threading.Thread] = []
        self._mixer_thread: threading.Thread | None = None

        self._acc: list[np.ndarray] = []          # akumulátor namixovaných vzorků (48 kHz)
        self._acc_frames = 0
        self._mixed_frames_total = 0              # celkem namixováno (48 kHz rámců)
        self._emitted_frames = 0                  # kolik rámců už odešlo přes on_chunk

        self._running = False
        self._device_error: Exception | None = None

    # ------------------------------------------------------------------ API

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Otevře zařízení a spustí zachytávací + mixovací vlákna.

        Při selhání zařízení vyhazuje ``RuntimeError`` s českou zprávou.
        """
        if self._running:
            return

        import soundcard as sc  # líný import — bezpečné mimo Windows

        try:
            speaker = sc.default_speaker()
            loopback_mic = sc.get_microphone(speaker.name, include_loopback=True)
            mic = sc.default_microphone()
        except Exception as exc:  # noqa: BLE001 - zařízení může selhat mnoha způsoby
            raise RuntimeError(f"Nepodařilo se otevřít zvukové zařízení: {exc}") from exc

        self._stop.clear()
        self._device_error = None
        self._mic_q.clear()
        self._loop_q.clear()
        self._acc = []
        self._acc_frames = 0
        self._mixed_frames_total = 0
        self._emitted_frames = 0

        t_loop = threading.Thread(
            target=self._capture_loop, args=(loopback_mic, self._loop_q, "loopback"),
            name="audio-loopback", daemon=True,
        )
        t_mic = threading.Thread(
            target=self._capture_loop, args=(mic, self._mic_q, "mikrofon"),
            name="audio-mic", daemon=True,
        )
        self._capture_threads = [t_loop, t_mic]
        self._mixer_thread = threading.Thread(
            target=self._mixer_loop, name="audio-mixer", daemon=True,
        )

        self._running = True
        for t in self._capture_threads:
            t.start()
        self._mixer_thread.start()

    def stop(self) -> float:
        """Zastaví zachytávání, dokončí mix, vypustí poslední částečný blok
        (je-li delší než 1 s) a vrátí celkovou délku nahrávky v sekundách."""
        if not self._running:
            return self._mixed_frames_total / NATIVE_RATE

        self._stop.set()
        for t in self._capture_threads:
            t.join(timeout=5.0)
        if self._mixer_thread is not None:
            self._mixer_thread.join(timeout=10.0)

        # Poslední částečný blok — vypustit, pokud má víc než 1 sekundu.
        if self._acc_frames > NATIVE_RATE:
            tail = (
                np.concatenate(self._acc)
                if self._acc
                else np.zeros((0, 2), np.float32)
            )
            self._emit(tail)
        self._acc = []
        self._acc_frames = 0

        self._running = False
        self._capture_threads = []
        self._mixer_thread = None
        return self._mixed_frames_total / NATIVE_RATE

    # ------------------------------------------------------------ vlákna

    def _capture_loop(self, device, sink: deque, label: str) -> None:
        try:
            with device.recorder(samplerate=NATIVE_RATE, blocksize=BLOCK_FRAMES) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=BLOCK_FRAMES)
                    mono = _to_mono_f32(data)
                    if len(mono) == 0:
                        continue
                    with self._buf_lock:
                        sink.append(mono)
        except Exception as exc:  # noqa: BLE001
            log.exception("Nepodařilo se otevřít zvukové zařízení (%s).", label)
            self._device_error = RuntimeError(
                f"Nepodařilo se otevřít zvukové zařízení: {exc}"
            )
            self._stop.set()

    def _mixer_loop(self) -> None:
        while True:
            capture_done = self._stop.is_set() and not any(
                t.is_alive() for t in self._capture_threads
            )
            block = self._next_block(allow_single=capture_done)
            if block is None:
                if capture_done:
                    break
                time.sleep(0.01)
                continue
            self._accumulate(block)

    # ------------------------------------------------------------- mixing

    def _next_block(self, allow_single: bool) -> np.ndarray | None:
        """Vezme jeden pár bloků (mic + loopback) zarovnaný podle pořadí
        a vrátí je jako STEREO pole shape (n, 2): [:, 0] = mikrofon (Ivan),
        [:, 1] = loopback (ostatní). Oddělené kanály umožňují atribuci
        mluvčích v post-processingu. Po konci zachytávání (``allow_single``)
        zpracuje i nespárované zbytky (chybějící kanál doplní tichem)."""
        with self._buf_lock:
            if self._mic_q and self._loop_q:
                mic = self._mic_q.popleft()
                loop = self._loop_q.popleft()
            elif allow_single and (self._mic_q or self._loop_q):
                if self._mic_q:
                    mic = self._mic_q.popleft()
                    loop = np.zeros_like(mic)
                else:
                    loop = self._loop_q.popleft()
                    mic = np.zeros_like(loop)
            else:
                return None

        n = max(len(mic), len(loop))
        if len(mic) < n:
            mic = np.pad(mic, (0, n - len(mic)))
        if len(loop) < n:
            loop = np.pad(loop, (0, n - len(loop)))
        stereo = np.stack(
            [np.clip(mic, -1.0, 1.0), np.clip(loop, -1.0, 1.0)], axis=1
        )
        return stereo.astype(np.float32)

    def _accumulate(self, block: np.ndarray) -> None:
        self._acc.append(block)
        self._acc_frames += len(block)
        self._mixed_frames_total += len(block)

        chunk_frames = int(self._cfg.chunk_seconds * NATIVE_RATE)
        while self._acc_frames >= chunk_frames:
            joined = np.concatenate(self._acc)
            chunk, rest = joined[:chunk_frames], joined[chunk_frames:]
            self._acc = [rest] if len(rest) else []
            self._acc_frames = len(rest)
            self._emit(chunk)

    def _emit(self, native_chunk: np.ndarray) -> None:
        """Převzorkuje a předá blok dál. Blok je stereo (n, 2):
        [:, 0] = mikrofon, [:, 1] = loopback."""
        offset_s = self._emitted_frames / NATIVE_RATE
        self._emitted_frames += len(native_chunk)
        if native_chunk.ndim == 1:  # defenzivně (starý mono formát)
            native_chunk = np.stack([native_chunk, native_chunk], axis=1)
        resampled = np.stack(
            [
                _resample_linear(native_chunk[:, 0], NATIVE_RATE, self._cfg.sample_rate),
                _resample_linear(native_chunk[:, 1], NATIVE_RATE, self._cfg.sample_rate),
            ],
            axis=1,
        )
        try:
            self._on_chunk(resampled, offset_s)
        except Exception:  # noqa: BLE001 - callback nesmí shodit zachytávání
            log.exception("Callback on_chunk selhal.")
