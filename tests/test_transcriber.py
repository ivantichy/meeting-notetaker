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
from app.transcriber import MAX_QUEUE_CHUNKS, Transcriber, model_is_downloaded


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


def test_live_transcribe_uses_multilingual_false_and_prompt_with_attendees(cfg):
    """Živý přepis volá model.transcribe s multilingual=False a initial_prompt,
    který obsahuje slovník (Claude) i jména účastníků předaná transcriberu."""
    captured = {}

    class CapturingModel(FakeModel):
        def transcribe(self, audio, **kw):
            captured.update(kw)
            return super().transcribe(audio, **kw)

    tr = Transcriber(
        cfg,
        on_segments=lambda segs: None,
        model_factory=CapturingModel,
        attendees=["Petr Novák", "ivan@example.com"],
    )
    tr.start()
    tr.submit(np.zeros(1600, dtype=np.float32), 0.0)
    assert _wait_until(lambda: "multilingual" in captured)
    tr.stop()

    assert captured["multilingual"] is False
    assert captured["language"] == "cs"  # konkrétní kód z configu
    assert "Claude" in captured["initial_prompt"]
    assert "Petr Novák" in captured["initial_prompt"]
    assert "ivan" in captured["initial_prompt"]  # e-mail -> lokální část
    assert captured["vad_parameters"]["min_speech_duration_ms"] == 250
    assert captured["vad_parameters"]["max_speech_duration_s"] == 30


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


# --------------------------------------------------------------- M9: detekce
# stahování modelu (indikátor "Stahuji model…" v UI).


def _make_snapshot(root, cache_name: str) -> None:
    """Vytvoří v ``root`` realistický HuggingFace cache snapshot s obsahem."""
    snap = root / cache_name / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "model.bin").write_bytes(b"\x00")
    (snap / "config.json").write_text("{}", encoding="utf-8")


def test_model_is_downloaded_false_when_absent(tmp_path):
    """Prázdný download_root -> model není stažený (UI má hlásit stahování)."""
    assert model_is_downloaded("small", str(tmp_path)) is False


def test_model_is_downloaded_true_when_snapshot_present(tmp_path):
    """Existující snapshot s obsahem -> model je k dispozici (žádné stahování)."""
    # "small" -> Systran/faster-whisper-small -> models--Systran--faster-whisper-small
    _make_snapshot(tmp_path, "models--Systran--faster-whisper-small")
    assert model_is_downloaded("small", str(tmp_path)) is True


def test_model_is_downloaded_post_model_repo_mapping(tmp_path):
    """large-v3-turbo se mapuje na mobiuslabsgmbh repo (jiná org než Systran)."""
    _make_snapshot(
        tmp_path, "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo"
    )
    assert model_is_downloaded("large-v3-turbo", str(tmp_path)) is True
    # jiný model ve stejném rootu chybí -> stahování
    assert model_is_downloaded("small", str(tmp_path)) is False


def test_model_is_downloaded_empty_snapshot_counts_as_absent(tmp_path):
    """Snapshot adresář bez souborů (nedokončené stažení) -> není hotovo."""
    (tmp_path / "models--Systran--faster-whisper-small" / "snapshots").mkdir(
        parents=True
    )
    assert model_is_downloaded("small", str(tmp_path)) is False


def test_model_is_downloaded_local_path_is_present(tmp_path):
    """Lokální cesta k modelu (adresář) -> nestahuje se."""
    assert model_is_downloaded(str(tmp_path)) is True


def test_model_is_downloaded_empty_name_is_present():
    """Prázdný název (vypnutý model) -> nic se nestahuje."""
    assert model_is_downloaded("") is True


def test_transcriber_reports_downloading_when_model_absent(cfg, monkeypatch):
    """M9: když model ještě není stažený, transcriber projde stavem
    'downloading' a po načtení skončí na 'ready'. faster_whisper je v testech
    mock (conftest), takže reálné stažení neproběhne — řídíme jen detekci."""
    import app.transcriber as tmod

    # Vynutíme "není stažený" bez závislosti na FS/CWD.
    monkeypatch.setattr(tmod, "model_is_downloaded", lambda name, root: False)

    states: list[str] = []
    # Reálná build cesta (model_factory=None) -> WhisperModel z mocku.
    tr = Transcriber(
        cfg,
        on_segments=lambda segs: None,
        model_factory=None,
    )
    tr.on_model_status = states.append
    assert tr.model_status == ""

    tr._get_model()  # přímo build cesta (jinak by ji spustil start())

    assert states[0] == "downloading"  # ohlášeno PŘED stavěním modelu
    assert tr.model_status == "ready"
    assert states[-1] == "ready"


def test_transcriber_reports_loading_when_model_present(cfg, monkeypatch):
    """Když je model už stažený, hlásí se 'loading' (ne 'downloading')."""
    import app.transcriber as tmod

    monkeypatch.setattr(tmod, "model_is_downloaded", lambda name, root: True)
    states: list[str] = []
    tr = Transcriber(cfg, on_segments=lambda segs: None, model_factory=None)
    tr.on_model_status = states.append
    tr._get_model()
    assert "downloading" not in states
    assert states[0] == "loading"
    assert tr.model_status == "ready"


def test_transcriber_model_status_lifecycle_with_injected_factory(cfg):
    """Injektovaný model (testy) -> loading -> ready, bez detekce FS."""
    states: list[str] = []
    tr = Transcriber(
        cfg, on_segments=lambda segs: None, model_factory=FakeModel
    )
    tr.on_model_status = states.append
    tr.start()
    tr.stop()
    assert states == ["loading", "ready"]
    assert tr.model_status == "ready"
