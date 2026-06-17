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
        self.on_device_error = None  # recorder ho nastaví na svůj handler (H5)

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



# ------------------------------------------- H8: WAV round-trip + kanály


def _read_wav(path):
    import wave

    with wave.open(path, "rb") as wf:
        params = (wf.getnchannels(), wf.getsampwidth(), wf.getframerate())
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    channels = audio.reshape(-1, params[0])
    return params, channels


def test_wav_roundtrip_stereo_channel_order(recorder, holder, meeting, cfg):
    """Recorder zapíše reálné STEREO; po přečtení zpět musí kanál 0 = mikrofon
    (Ivan), kanál 1 = loopback (ostatní). Záměna kanálů by přeznačila mluvčí."""
    recorder.start(meeting)
    wav_path = recorder.wav_path
    assert wav_path is not None

    n = cfg.sample_rate  # 1 s
    mic = np.full(n, 0.5, dtype=np.float32)       # mikrofon hlasitý
    loop = np.full(n, -0.25, dtype=np.float32)    # loopback tišší, opačná polarita
    stereo = np.stack([mic, loop], axis=1)
    holder.capture.emit_chunk(stereo, 0.0)

    recorder.stop()

    (n_channels, sampwidth, framerate), channels = _read_wav(wav_path)
    assert n_channels == 2
    assert sampwidth == 2                 # 16bit PCM
    assert framerate == cfg.sample_rate
    assert channels.shape == (n, 2)
    # kanál 0 ~ mic (0.5), kanál 1 ~ loopback (-0.25) — pořadí zachováno
    assert float(np.mean(channels[:, 0])) == pytest.approx(0.5, abs=1e-3)
    assert float(np.mean(channels[:, 1])) == pytest.approx(-0.25, abs=1e-3)


def test_wav_roundtrip_clips_out_of_range(recorder, holder, meeting, cfg):
    recorder.start(meeting)
    wav_path = recorder.wav_path
    n = cfg.sample_rate
    loud = np.stack(
        [np.full(n, 2.0, dtype=np.float32), np.full(n, -2.0, dtype=np.float32)],
        axis=1,
    )
    holder.capture.emit_chunk(loud, 0.0)
    recorder.stop()
    _, channels = _read_wav(wav_path)
    assert channels.max() <= 1.0 and channels.min() >= -1.0
    assert float(np.mean(channels[:, 0])) == pytest.approx(1.0, abs=1e-3)


def test_device_error_callback_propagates(recorder, holder, meeting):
    """H5: výpadek zařízení (capture.on_device_error) probublá do recorder
    callbacku on_device_error."""
    seen = []
    recorder.on_device_error.append(seen.append)
    recorder.start(meeting)
    # FakeCapture nedostane on_device_error atribut nastavený? Recorder ho
    # nastaví jen pokud existuje — přidáme ho a simulujeme výpadek.
    assert hasattr(holder.capture, "on_device_error")
    holder.capture.on_device_error("Zvukové zařízení selhalo (mikrofon): test")
    assert seen == ["Zvukové zařízení selhalo (mikrofon): test"]
    recorder.stop()


# ----------------------------------- B: tematické termíny do živého přepisu


class _KwTranscriber(FakeTranscriber):
    """Transcriber, který přijme kwargs attendees/title/topic_terms a uloží je
    (mirror reálného Transcriberu) — ověříme, co recorder předá."""

    def __init__(self, cfg, on_segments, attendees=None, title=None, topic_terms=None):
        super().__init__(cfg, on_segments)
        self.attendees = list(attendees or [])
        self.title = title
        self.topic_terms = list(topic_terms or [])


def test_live_recorder_passes_topic_terms_to_transcriber(cfg, store):
    """Živý přepis: recorder vytěží tematické termíny z názvu + popisu schůzky
    a předá je Transcriberu (kvalitnější přepis názvů)."""
    captured = {}

    def transcriber_factory(cfg_, on_segments, **kw):
        tr = _KwTranscriber(cfg_, on_segments, **kw)
        captured["tr"] = tr
        return tr

    rec = Recorder(
        cfg, store,
        capture_factory=lambda c, on_chunk: FakeCapture(c, on_chunk),
        transcriber_factory=transcriber_factory,
    )
    start = datetime(2026, 6, 12, 13, 30).astimezone()
    m = Meeting(
        uid="topic-1::x",
        title="Migrace na PowerShell",
        start=start,
        end=start + timedelta(hours=1),
        platform=Platform.MEET,
        attendees=["ivan@example.com"],
        description="Nasadíme GitHub a elem6, napojíme MCP server.",
    )
    rec.start(m)
    tr = captured["tr"]
    # termíny z názvu + popisu dorazily do transcriberu
    assert "PowerShell" in tr.topic_terms
    assert "GitHub" in tr.topic_terms
    assert "elem6" in tr.topic_terms
    assert "MCP" in tr.topic_terms
    # jména a název se předávají dál jako dřív
    assert tr.title == "Migrace na PowerShell"
    rec.stop()


def test_manual_recording_has_empty_topic_terms(cfg, store):
    """Ruční záznam (syntetická schůzka bez popisu) -> žádné tematické termíny,
    chování jako dřív (jen base + slovník)."""
    captured = {}

    def transcriber_factory(cfg_, on_segments, **kw):
        tr = _KwTranscriber(cfg_, on_segments, **kw)
        captured["tr"] = tr
        return tr

    rec = Recorder(
        cfg, store,
        capture_factory=lambda c, on_chunk: FakeCapture(c, on_chunk),
        transcriber_factory=transcriber_factory,
    )
    rec.start_manual()
    tr = captured["tr"]
    assert tr.topic_terms == []  # "Ruční záznam" -> žádné identifikátory
    rec.stop()
