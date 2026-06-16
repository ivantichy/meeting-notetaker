"""Testy živého přepisu (Transcriber) — H8 / H1.

Reálné faster-whisper se nahrazuje injektovaným fake modelem (model_factory),
takže testy běží bez knihovny i bez CPU zátěže. Pokrývají: drain při stop,
přežití chyby bloku, tvrdé selhání načtení modelu (H1) a strop fronty s
zahazováním nejstaršího bloku (H1).
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from app.config import AppConfig
from app.transcriber import MAX_QUEUE_CHUNKS, Transcriber


def _wait_until(cond, timeout: float = 5.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


class _Seg:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class FakeModel:
    """Fake WhisperModel: pro každý blok vrátí jeden segment 'ok'."""

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        return [_Seg(0.0, 1.0, "ok")], {"language": "cs"}


@pytest.fixture
def cfg():
    return AppConfig(language="cs", chunk_seconds=1, sample_rate=16000)


def test_processes_chunk_applies_offset(cfg):
    out = []
    model = FakeModel()
    tr = Transcriber(
        cfg,
        on_segments=lambda segs: out.extend(segs),
        model_factory=lambda: model,
    )
    tr.start()
    tr.submit(np.zeros(16000, dtype=np.float32), offset_s=20.0)
    assert _wait_until(lambda: len(out) >= 1)
    tr.stop()
    t0, t1, text = out[0]
    assert text == "ok"
    assert t0 == pytest.approx(20.0)  # offset se přičetl
    assert t1 == pytest.approx(21.0)


def test_stop_drains_remaining_queue(cfg):
    out = []
    # pomalý model, ať se ve frontě stihne nahromadit víc bloků
    class SlowModel(FakeModel):
        def transcribe(self, audio, **kw):
            time.sleep(0.02)
            return super().transcribe(audio, **kw)

    tr = Transcriber(
        cfg, on_segments=lambda segs: out.extend(segs), model_factory=SlowModel
    )
    tr.start()
    for i in range(5):
        tr.submit(np.zeros(1600, dtype=np.float32), offset_s=float(i))
    tr.stop(drain=True)  # musí dozpracovat všechny
    assert len(out) == 5


def test_worker_survives_bad_chunk(cfg):
    out = []
    calls = {"n": 0}

    class FlakyModel:
        def transcribe(self, audio, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("blok selhal")
            return [_Seg(0.0, 1.0, "po chybě")], {}

    tr = Transcriber(
        cfg, on_segments=lambda segs: out.extend(segs), model_factory=FlakyModel
    )
    tr.start()
    tr.submit(np.zeros(1600, dtype=np.float32), 0.0)  # spadne
    tr.submit(np.zeros(1600, dtype=np.float32), 1.0)  # projde
    assert _wait_until(lambda: any(s[2] == "po chybě" for s in out))
    tr.stop()


def test_model_load_failure_raises_and_reports(cfg):
    """H1: selhání načtení modelu se tvrdě ohlásí (on_error + výjimka), ne ticho."""
    errors = []

    def boom_factory():
        raise RuntimeError("model se nestáhl")

    tr = Transcriber(
        cfg,
        on_segments=lambda segs: None,
        on_error=lambda msg: errors.append(msg),
        model_factory=boom_factory,
    )
    with pytest.raises(RuntimeError, match="model se nestáhl"):
        tr.start()
    assert errors and "model se nestáhl" in errors[0]


def test_queue_is_bounded_drops_oldest(cfg):
    """H1: fronta má strop; při zahlcení se zahazuje nejstarší blok, neroste."""
    # model nikdy nespustíme (žádný worker nekonzumuje), jen plníme frontu
    tr = Transcriber(cfg, on_segments=lambda segs: None, model_factory=FakeModel)
    for i in range(MAX_QUEUE_CHUNKS + 50):
        tr.submit(np.zeros(10, dtype=np.float32), float(i))
    assert tr.queue_depth <= MAX_QUEUE_CHUNKS
