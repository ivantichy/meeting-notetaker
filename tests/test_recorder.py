"""Testy pro app.recorder.Recorder — s fake capture/transcriber a reálným NoteStore.

conftest.py instaluje sys.modules mocky pro `soundcard` a `faster_whisper`
před importem app modulů; zde navíc injektujeme fake objekty přes factories,
takže se reálné AudioCapture/Transcriber vůbec nevytvářejí.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.config import AppConfig
from app.models import Meeting, Platform, RecorderState
from app.recorder import Recorder
from app.storage import NoteStore


# ------------------------------------------------------------------- fakes


class FakeCapture:
    """Fake AudioCapture: on_chunk voláme synchronně přes emit_chunk()."""

    def __init__(self, cfg, on_chunk):
        self.cfg = cfg
        self.on_chunk = on_chunk
        self.started = False
        self.stopped = False
        self.duration = 123.0
        self._running = False

    def start(self):
        self.started = True
        self._running = True

    def stop(self):
        self.stopped = True
        self._running = False
        return self.duration

    @property
    def is_running(self):
        return self._running

    def emit_chunk(self, samples, offset_s):
        self.on_chunk(samples, offset_s)


class FakeTranscriber:
    """Fake Transcriber: eviduje submity, on_segments voláme ručně z testu."""

    def __init__(self, cfg, on_segments):
        self.cfg = cfg
        self.on_segments = on_segments
        self.started = False
        self.submissions = []
        self.stop_calls = []

    def start(self):
        self.started = True

    def submit(self, samples, offset_s):
        self.submissions.append((samples, offset_s))

    def stop(self, drain=True):
        self.stop_calls.append(drain)

    @property
    def queue_depth(self):
        return len(self.submissions)


class Holder:
    """Drží instance vytvořené factories, aby na ně testy dosáhly."""

    capture: FakeCapture | None = None
    transcriber: FakeTranscriber | None = None


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(notes_dir=str(tmp_path))


@pytest.fixture
def store(tmp_path):
    return NoteStore(str(tmp_path))


@pytest.fixture
def meeting():
    start = datetime(2026, 6, 12, 13, 30).astimezone()
    return Meeting(
        uid="abc123@google.com::2026-06-12T13:30",
        title="Týmová porada",
        start=start,
        end=start + timedelta(hours=1),
        platform=Platform.MEET,
        join_url="https://meet.google.com/abc-defg-hij",
        attendees=["ivan@example.com"],
    )


@pytest.fixture
def holder():
    return Holder()


@pytest.fixture
def recorder(cfg, store, holder):
    def capture_factory(cfg_, on_chunk):
        holder.capture = FakeCapture(cfg_, on_chunk)
        return holder.capture

    def transcriber_factory(cfg_, on_segments):
        holder.transcriber = FakeTranscriber(cfg_, on_segments)
        return holder.transcriber

    return Recorder(
        cfg,
        store,
        capture_factory=capture_factory,
        transcriber_factory=transcriber_factory,
    )


# ------------------------------------------------------------------- testy


def test_happy_path_start_chunks_segments_stop(recorder, holder, meeting, cfg):
    states = []
    received = []
    recorder.on_state_changed.append(states.append)
    recorder.on_segment.append(lambda t0, t1, text: received.append((t0, t1, text)))

    # --- start ---
    recorder.start(meeting)
    assert recorder.state is RecorderState.RECORDING
    assert recorder.current_meeting is meeting
    assert states == [RecorderState.RECORDING]
    assert holder.capture is not None and holder.capture.started
    assert holder.transcriber is not None and holder.transcriber.started

    note_path = recorder.note_path
    assert note_path is not None
    assert Path(note_path).exists()
    initial = Path(note_path).read_text(encoding="utf-8")
    assert "status: recording" in initial
    assert "## Přepis" in initial

    # --- chunky tečou do transcriberu ---
    chunk = np.zeros(cfg.sample_rate * 2, dtype=np.float32)
    holder.capture.emit_chunk(chunk, 0.0)
    holder.capture.emit_chunk(chunk, 20.0)
    assert [offset for _, offset in holder.transcriber.submissions] == [0.0, 20.0]

    # --- segmenty z transcriberu -> soubor + callback ---
    holder.transcriber.on_segments(
        [(5.0, 8.0, "Dobrý den, vítejte."), (65.0, 70.0, "Začneme agendou.")]
    )
    assert received == [
        (5.0, 8.0, "Dobrý den, vítejte."),
        (65.0, 70.0, "Začneme agendou."),
    ]
    content = Path(note_path).read_text(encoding="utf-8")
    assert "[00:00:05] Dobrý den, vítejte." in content
    assert "[00:01:05] Začneme agendou." in content

    # --- stop finalizuje ---
    recorder.stop()
    assert holder.capture.stopped
    assert recorder.state is RecorderState.IDLE
    assert recorder.current_meeting is None
    assert states == [
        RecorderState.RECORDING,
        RecorderState.FINALIZING,
        RecorderState.IDLE,
    ]
    final = Path(note_path).read_text(encoding="utf-8")
    assert "status: done" in final
    assert "status: recording" not in final
    assert "duration" in final
    # přepis zůstal zachován
    assert "[00:00:05] Dobrý den, vítejte." in final


def test_manual_recording(recorder, holder):
    recorder.start_manual()

    m = recorder.current_meeting
    assert m is not None
    assert m.uid.startswith("manual:")
    assert m.title == "Ruční záznam"
    assert m.platform is Platform.OTHER
    assert m.end - m.start == timedelta(hours=2)
    assert m.start.tzinfo is not None
    assert recorder.state is RecorderState.RECORDING
    assert recorder.note_path is not None

    recorder.stop()
    assert recorder.state is RecorderState.IDLE


def test_double_start_raises(recorder, holder, meeting):
    recorder.start(meeting)
    with pytest.raises(RuntimeError):
        recorder.start(meeting)
    # první nahrávání běží dál nedotčené
    assert recorder.state is RecorderState.RECORDING
    assert recorder.current_meeting is meeting


def test_stop_drains_transcriber(recorder, holder, meeting):
    recorder.start(meeting)
    chunk = np.zeros(16000, dtype=np.float32)
    holder.capture.emit_chunk(chunk, 0.0)

    recorder.stop()

    # stop musí vyžádat dovyprázdnění fronty: stop(drain=True), právě jednou
    assert holder.transcriber.stop_calls == [True]
    # a capture se zastavil dřív, než se finalizovalo
    assert holder.capture.stopped
    assert recorder.state is RecorderState.IDLE
