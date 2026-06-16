"""Testy finálního dopřepisu (PostProcessor) — reálný NoteStore + fake transcribe."""
import os
import time
import wave
from datetime import timedelta

import pytest

from app.config import AppConfig
from app.post_processor import PostProcessor
from app.storage import NoteStore


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_wav(path: str, seconds: float = 1.0, rate: int = 16000, channels: int = 1) -> None:
    """Malý reálný WAV: 16 kHz, 16bit, samé nuly."""
    n_frames = int(seconds * rate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * n_frames * channels)


def _wait_until(cond, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Deterministické čekání: polluje podmínku až do timeoutu."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def _make_note_with_wav(store, make_meeting, start, title):
    """Vytvoří poznámku přes NoteStore + stejnojmenný WAV; vrací (md_path, wav_path)."""
    meeting = make_meeting(start, title=title)
    note_path = store.create_note(meeting)
    store.append_segment(note_path, 0.0, 2.0, "Živý nekvalitní přepis.")
    wav_path = note_path[:-3] + ".wav"
    _write_wav(wav_path)
    return note_path, wav_path


@pytest.fixture
def cfg() -> AppConfig:
    return AppConfig(post_model="large-v3-turbo")


def test_happy_path_replaces_transcript_and_deletes_wav(
    tmp_notes_dir, make_meeting, fixed_now, cfg
):
    store = NoteStore(tmp_notes_dir)
    note_path, wav_path = _make_note_with_wav(
        store, make_meeting, fixed_now, "Týmová porada"
    )
    events = []
    fixed_segments = [
        (0.0, 2.5, "Kvalitní první věta."),
        (3.0, 6.0, "Kvalitní druhá věta."),
    ]
    pp = PostProcessor(
        cfg,
        store,
        transcribe_factory=lambda: (lambda audio: fixed_segments),
        on_event=lambda event, detail: events.append((event, detail)),
    )
    pp.start()
    pp.enqueue(note_path, wav_path)

    assert _wait_until(lambda: len(events) >= 2), "worker úkol nedokončil do 5 s"
    pp.stop()

    text = _read(note_path)
    # nové řádky v sekci Přepis, staré pryč
    assert "[00:00:00] Kvalitní první věta." in text
    assert "[00:00:03] Kvalitní druhá věta." in text
    assert "Živý nekvalitní přepis." not in text
    assert text.index("## Přepis") < text.index("[00:00:00] Kvalitní první věta.")
    # frontmatter má transcript_quality: final
    frontmatter = text.split("\n---\n", 1)[0]
    assert "transcript_quality: final" in frontmatter
    # WAV smazán
    assert not os.path.exists(wav_path)
    # události v pořadí START -> HOTOVO
    assert [e for e, _ in events] == ["PŘEPIS START", "PŘEPIS HOTOVO"]
    basename = os.path.basename(note_path)
    assert events[0][1] == basename
    assert events[1][1].startswith(basename)
    assert "1s audia" in events[1][1]


def test_transcribe_error_keeps_wav_and_worker_survives(
    tmp_notes_dir, make_meeting, fixed_now, cfg
):
    store = NoteStore(tmp_notes_dir)
    bad_note, bad_wav = _make_note_with_wav(
        store, make_meeting, fixed_now, "Vadná schůzka"
    )
    good_note, good_wav = _make_note_with_wav(
        store, make_meeting, fixed_now + timedelta(hours=2), "Dobrá schůzka"
    )
    bad_text_before = _read(bad_note)

    calls = {"n": 0}

    def factory():
        def fake_transcribe(audio):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("model selhal")
            return [(0.0, 1.5, "Náhradní finální věta.")]

        return fake_transcribe

    events = []
    pp = PostProcessor(
        cfg,
        store,
        transcribe_factory=factory,
        on_event=lambda event, detail: events.append((event, detail)),
    )
    pp.start()
    pp.enqueue(bad_note, bad_wav)   # první úkol spadne
    pp.enqueue(good_note, good_wav)  # druhý musí projít

    assert _wait_until(
        lambda: any(e == "PŘEPIS HOTOVO" for e, _ in events)
    ), "worker po chybě nepřežil / nedokončil druhý úkol"
    pp.stop()

    # vadný úkol: WAV NEsmazán, poznámka beze změny
    assert os.path.exists(bad_wav)
    assert _read(bad_note) == bad_text_before
    assert "Živý nekvalitní přepis." in _read(bad_note)
    # ohlášena chyba s názvem souboru a výjimkou
    error_events = [(e, d) for e, d in events if e == "PŘEPIS CHYBA"]
    assert len(error_events) == 1
    assert os.path.basename(bad_note) in error_events[0][1]
    assert "model selhal" in error_events[0][1]
    # dobrý úkol prošel: WAV smazán, přepis nahrazen
    assert not os.path.exists(good_wav)
    assert "[00:00:00] Náhradní finální věta." in _read(good_note)
    # pořadí událostí: START, CHYBA, START, HOTOVO
    assert [e for e, _ in events] == [
        "PŘEPIS START",
        "PŘEPIS CHYBA",
        "PŘEPIS START",
        "PŘEPIS HOTOVO",
    ]


def test_enqueue_noop_when_post_model_empty(tmp_notes_dir):
    store = NoteStore(tmp_notes_dir)
    cfg = AppConfig(post_model="")
    pp = PostProcessor(
        cfg, store, transcribe_factory=lambda: (lambda audio: [])
    )
    pp.enqueue("nejaka.md", "nejaka.wav")
    assert pp.pending == 0


def test_scan_orphans_enqueues_only_wavs_with_md(tmp_notes_dir, cfg):
    store = NoteStore(tmp_notes_dir)  # vytvoří adresář
    # a.wav + a.md -> kandidát; b.wav bez .md -> ignorovat; c.md bez .wav -> nic
    _write_wav(os.path.join(tmp_notes_dir, "a.wav"))
    with open(os.path.join(tmp_notes_dir, "a.md"), "w", encoding="utf-8") as f:
        f.write("---\nstatus: done\n---\n\n## Přepis\n")
    _write_wav(os.path.join(tmp_notes_dir, "b.wav"))
    with open(os.path.join(tmp_notes_dir, "c.md"), "w", encoding="utf-8") as f:
        f.write("---\nstatus: done\n---\n\n## Přepis\n")

    pp = PostProcessor(
        cfg, store, transcribe_factory=lambda: (lambda audio: [])
    )
    assert pp.scan_orphans(tmp_notes_dir) == 1
    assert pp.pending == 1


def test_speaker_attribution_from_stereo_channels():
    """Energie v mic kanálu -> Ivan, v loopback kanálu -> Ostatní."""
    import numpy as np

    from app.post_processor import _attribute_speakers

    sr = 16000
    n = sr * 4  # 4 s
    mic = np.zeros(n, dtype=np.float32)
    loop = np.zeros(n, dtype=np.float32)
    mic[: sr * 2] = 0.5      # 0-2 s mluví Ivan
    loop[sr * 2 :] = 0.5     # 2-4 s mluví protistrana
    channels = np.stack([mic, loop], axis=1)

    segments = [(0.0, 1.8, "Ahoj, slyšíme se?"), (2.2, 3.8, "Slyšíme, ahoj.")]
    labeled = _attribute_speakers(channels, sr, segments)
    assert labeled[0][3] == "Ivan"
    assert labeled[1][3] == "Ostatní"


def test_speaker_attribution_mono_returns_none():
    import numpy as np

    from app.post_processor import _attribute_speakers

    channels = np.zeros((16000, 1), dtype=np.float32)
    labeled = _attribute_speakers(channels, 16000, [(0.0, 0.5, "x")])
    assert labeled[0][3] is None
