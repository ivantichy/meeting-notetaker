"""Testy zachytávání zvuku (audio_capture) — H8.

Pokrývají dosud netestovaný, matematicky náročný kód: převzorkování,
převod na mono, párování mic/loopback bloků a skládání STEREO chunků
(kanál 0 = mikrofon/Ivan, kanál 1 = loopback/ostatní) i offsety a emit.
Mixovací/zachytávací smyčky řídíme ručně bez reálných vláken a zařízení.
"""
from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from app.audio_capture import (
    BLOCK_FRAMES,
    NATIVE_RATE,
    AudioCapture,
    _resample_linear,
    _to_mono_f32,
)
from app.config import AppConfig


# ---------------------------------------------------------- _resample_linear


class TestResampleLinear:
    def test_identity_same_rate(self):
        x = np.array([0.0, 0.5, -0.5, 1.0], dtype=np.float32)
        out = _resample_linear(x, 48000, 48000)
        assert np.array_equal(out, x)
        assert out.dtype == np.float32

    def test_empty_input(self):
        out = _resample_linear(np.zeros(0, dtype=np.float32), 48000, 16000)
        assert len(out) == 0

    def test_downsample_length_48k_to_16k(self):
        x = np.zeros(48000, dtype=np.float32)  # 1 s @ 48k
        out = _resample_linear(x, 48000, 16000)
        assert len(out) == 16000  # 1 s @ 16k
        assert out.dtype == np.float32

    def test_endpoints_preserved(self):
        # lineární rampa 0..1; po převzorkování zůstanou koncové body ~stejné
        x = np.linspace(0.0, 1.0, 48000, dtype=np.float32)
        out = _resample_linear(x, 48000, 16000)
        assert out[0] == pytest.approx(0.0, abs=1e-3)
        assert out[-1] == pytest.approx(1.0, abs=1e-3)

    def test_constant_signal_stays_constant(self):
        x = np.full(48000, 0.3, dtype=np.float32)
        out = _resample_linear(x, 48000, 16000)
        assert np.allclose(out, 0.3, atol=1e-4)


# -------------------------------------------------------------- _to_mono_f32


class TestToMonoF32:
    def test_mono_passthrough(self):
        x = np.array([0.1, 0.2, 0.3], dtype=np.float32)
        out = _to_mono_f32(x)
        assert np.allclose(out, x)
        assert out.dtype == np.float32

    def test_stereo_averaged(self):
        x = np.array([[0.0, 1.0], [0.5, 0.5], [-1.0, 1.0]], dtype=np.float32)
        out = _to_mono_f32(x)
        assert np.allclose(out, [0.5, 0.5, 0.0])

    def test_single_column_2d(self):
        x = np.array([[0.1], [0.2]], dtype=np.float32)
        out = _to_mono_f32(x)
        assert np.allclose(out, [0.1, 0.2])


# ------------------------------------------- _next_block: párování a kanály


@pytest.fixture
def cap():
    cfg = AppConfig(sample_rate=16000, chunk_seconds=1)
    return AudioCapture(cfg, on_chunk=lambda samples, offset: None)


class TestNextBlock:
    def test_pairs_mic_left_loop_right(self, cap):
        # mic = 0.4 (konstantní), loopback = -0.4 -> kanál 0 musí být mic
        cap._mic_q = deque([np.full(10, 0.4, dtype=np.float32)])
        cap._loop_q = deque([np.full(10, -0.4, dtype=np.float32)])
        block = cap._next_block(allow_single=False)
        assert block.shape == (10, 2)
        assert np.allclose(block[:, 0], 0.4)   # mikrofon = L
        assert np.allclose(block[:, 1], -0.4)  # loopback = R

    def test_none_when_one_queue_empty_and_not_allow_single(self, cap):
        cap._mic_q = deque([np.zeros(10, dtype=np.float32)])
        cap._loop_q = deque()
        assert cap._next_block(allow_single=False) is None

    def test_allow_single_pads_missing_loop_with_silence(self, cap):
        cap._mic_q = deque([np.full(8, 0.5, dtype=np.float32)])
        cap._loop_q = deque()
        block = cap._next_block(allow_single=True)
        assert block.shape == (8, 2)
        assert np.allclose(block[:, 0], 0.5)
        assert np.allclose(block[:, 1], 0.0)  # chybějící loopback = ticho

    def test_clips_out_of_range(self, cap):
        cap._mic_q = deque([np.array([2.0, -2.0], dtype=np.float32)])
        cap._loop_q = deque([np.array([2.0, -2.0], dtype=np.float32)])
        block = cap._next_block(allow_single=False)
        assert block.max() <= 1.0 and block.min() >= -1.0

    def test_unequal_lengths_padded_to_max(self, cap):
        cap._mic_q = deque([np.full(5, 0.2, dtype=np.float32)])
        cap._loop_q = deque([np.full(3, 0.3, dtype=np.float32)])
        block = cap._next_block(allow_single=False)
        assert block.shape == (5, 2)
        assert np.allclose(block[3:, 1], 0.0)  # kratší loopback doplněn tichem


# ------------------------------------ _accumulate/_emit: offsety, převzorkování


class TestAccumulateEmit:
    def test_emits_on_full_chunk_with_correct_offsets(self, cap):
        emitted = []
        cap._on_chunk = lambda samples, offset: emitted.append((samples, offset))
        # chunk_seconds=1 -> 48000 rámců na chunk; pošleme 2.5 chunku
        chunk_frames = int(cap._cfg.chunk_seconds * NATIVE_RATE)
        block = np.zeros((chunk_frames, 2), dtype=np.float32)
        cap._accumulate(block)        # 1. chunk
        cap._accumulate(block)        # 2. chunk
        cap._accumulate(block[: chunk_frames // 2])  # zbytek (0.5) -> neemit
        assert len(emitted) == 2
        # offsety navazují: 0 s a 1 s (po převzorkování na 16k)
        assert emitted[0][1] == pytest.approx(0.0)
        assert emitted[1][1] == pytest.approx(1.0)
        # každý emitnutý chunk je stereo, převzorkovaný na sample_rate
        assert emitted[0][0].shape == (cap._cfg.sample_rate, 2)
        assert emitted[0][0].dtype == np.float32

    def test_emit_preserves_channel_separation_after_resample(self, cap):
        emitted = []
        cap._on_chunk = lambda samples, offset: emitted.append(samples)
        n = NATIVE_RATE  # 1 s
        mic = np.full(n, 0.6, dtype=np.float32)
        loop = np.full(n, -0.2, dtype=np.float32)
        stereo = np.stack([mic, loop], axis=1)
        cap._emit(stereo)
        out = emitted[0]
        assert out.shape == (cap._cfg.sample_rate, 2)
        assert np.allclose(out[:, 0], 0.6, atol=1e-3)   # mic kanál zachován
        assert np.allclose(out[:, 1], -0.2, atol=1e-3)  # loopback kanál zachován

    def test_block_frames_constant_is_half_second(self):
        # BLOCK_FRAMES = 0.5 s @ 48k (kontrakt s recorderem/komentářem)
        assert BLOCK_FRAMES == NATIVE_RATE // 2


# -------------------------------------------------- výpadek zařízení (H5)


class TestDeviceErrorCallback:
    def test_capture_loop_error_invokes_callback_once(self, cap):
        seen = []
        cap.on_device_error = lambda msg: seen.append(msg)

        class BoomDevice:
            def recorder(self, **kw):
                raise RuntimeError("zařízení zmizelo")

        # _capture_loop chytí výjimku z .recorder(), nastaví _device_error,
        # zastaví běh a jednou zavolá callback.
        cap._capture_threads = []
        cap._capture_loop(BoomDevice(), deque(), "mikrofon")
        assert cap._stop.is_set()
        assert cap._device_error is not None
        assert len(seen) == 1
        assert "mikrofon" in seen[0]
