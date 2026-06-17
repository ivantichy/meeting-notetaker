"""Testy finálního dopřepisu (PostProcessor) — reálný NoteStore + fake transcribe."""
import os
import time
import wave
from datetime import timedelta
from unittest import mock

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
    # Mezera 1.5 s (>= MERGE_GAP_S) -> _merge_segments je nechá jako 2 řádky.
    fixed_segments = [
        (0.0, 2.5, "Kvalitní první věta."),
        (4.0, 6.0, "Kvalitní druhá věta."),
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
    assert "[00:00:04] Kvalitní druhá věta." in text
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


# --------------------------------------------------------- _merge_segments


def test_merge_same_speaker_small_gap_joins():
    """Sousední segmenty téhož mluvčího s malou mezerou se slijí do jednoho."""
    from app.post_processor import _merge_segments

    segs = [
        (0.0, 1.0, "A to", "Ivan"),
        (1.5, 2.0, "ještě jako", "Ivan"),  # mezera 0.5 s < 1.2 s
    ]
    out = _merge_segments(segs)
    assert len(out) == 1
    assert out[0] == (0.0, 2.0, "A to ještě jako", "Ivan")


def test_merge_verbatim_duplicate_neighbor_dropped():
    """Doslovně zopakovaný soused (case/whitespace-insensitivně) se zahodí."""
    from app.post_processor import _merge_segments

    segs = [
        (0.0, 1.0, "A to ještě jako...", "Ivan"),
        (3.0, 4.0, "  a to JEŠTĚ jako...  ", "Ivan"),  # velká mezera, ale duplicita
        (6.0, 7.0, "Jiná věta.", "Ivan"),  # > MERGE_GAP_S za duplicitou -> samostatně
    ]
    out = _merge_segments(segs)
    texts = [s[2] for s in out]
    assert texts == ["A to ještě jako...", "Jiná věta."]
    # rozsah duplicitního opakování se promítne do end prvního segmentu
    assert out[0][1] == 4.0


def test_merge_different_speakers_not_merged():
    """Přes různé mluvčí se neslučuje ani při malé mezeře."""
    from app.post_processor import _merge_segments

    segs = [
        (0.0, 1.0, "Ahoj.", "Ivan"),
        (1.2, 2.0, "Ahoj zpět.", "Ostatní"),  # mezera 0.2 s, ale jiný mluvčí
    ]
    out = _merge_segments(segs)
    assert len(out) == 2
    assert out[0][3] == "Ivan"
    assert out[1][3] == "Ostatní"


def test_merge_gap_at_or_above_threshold_not_merged():
    """Mezera >= MERGE_GAP_S se neslučuje (zůstávají samostatné segmenty)."""
    from app.post_processor import MERGE_GAP_S, _merge_segments

    segs = [
        (0.0, 1.0, "První.", "Ivan"),
        (1.0 + MERGE_GAP_S, 2.0 + MERGE_GAP_S, "Druhá.", "Ivan"),
    ]
    out = _merge_segments(segs)
    assert len(out) == 2


def test_merge_preserves_unrelated_sequence():
    """Smysluplná posloupnost (různé texty, větší mezery) zůstane beze změny."""
    from app.post_processor import _merge_segments

    segs = [
        (0.0, 2.0, "Věta jedna.", "Ivan"),
        (5.0, 7.0, "Věta dvě.", "Ostatní"),
        (10.0, 12.0, "Věta tři.", "Ivan"),
    ]
    out = _merge_segments(segs)
    assert out == segs


# --------------------------------------------------- build_initial_prompt


def test_initial_prompt_includes_glossary_terms_and_attendees():
    """Prompt obsahuje termíny ze slovníku i jména účastníků (email -> lokální
    část). Vestavěný slovník neexistuje — termín zapíšeme do glossary.txt v cwd
    (testy běží v izolovaném pracovním adresáři, viz _isolate_cwd fixture)."""
    import pathlib

    from app.glossary import build_initial_prompt

    pathlib.Path("glossary.txt").write_text("elem6\nKubernetes\n", encoding="utf-8")
    prompt = build_initial_prompt(["ivan@example.com", "Petr Novák"])
    assert "Kubernetes" in prompt
    assert "elem6" in prompt
    assert "ivan" in prompt          # z e-mailu jen lokální část
    assert "Petr Novák" in prompt


def test_initial_prompt_deduplicates_case_insensitively():
    """Duplicitní položky (i přes velikost písmen) se v promptu neopakují."""
    from app.glossary import build_initial_prompt

    prompt = build_initial_prompt(["Claude", "claude", "ivan@x.cz", "ivan@y.cz"])
    assert prompt.lower().count("claude") == 1
    assert prompt.count("ivan") == 1


def test_default_transcribe_passes_multilingual_false_and_prompt():
    """_build_default_transcribe volá model.transcribe s multilingual=False,
    vad_parameters a initial_prompt obsahujícím slovník (termín z glossary.txt)."""
    import pathlib
    from unittest.mock import MagicMock

    import faster_whisper

    import app.post_processor as pp

    # Vestavěný slovník neexistuje — termín zapíšeme do glossary.txt v cwd
    # (testy běží v izolovaném pracovním adresáři, viz _isolate_cwd fixture).
    pathlib.Path("glossary.txt").write_text("Kubernetes\n", encoding="utf-8")

    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([], object())
    cfg = AppConfig(post_model="large-v3-turbo", language="auto")

    # _build_default_transcribe dělá `from faster_whisper import WhisperModel`
    # (conftest má faster_whisper jako MagicMock v sys.modules) — patchneme tedy
    # atribut WhisperModel, ať vrátí náš fake model.
    with mock.patch.object(faster_whisper, "WhisperModel", return_value=fake_model):
        transcribe = pp._build_default_transcribe(cfg, attendees=["Petr"])
    transcribe(object())  # bez per-call promptu -> fallback z buildu

    _, kwargs = fake_model.transcribe.call_args
    assert kwargs["multilingual"] is False
    assert kwargs["language"] is None  # "auto" -> detekce jednou
    assert "Kubernetes" in kwargs["initial_prompt"]  # termín z glossary.txt
    assert "Petr" in kwargs["initial_prompt"]
    assert kwargs["vad_parameters"]["min_speech_duration_ms"] == 250
    assert kwargs["vad_parameters"]["max_speech_duration_s"] == 30


def test_default_transcribe_per_call_prompt_overrides_fallback():
    """initial_prompt předaný do volání má přednost před promptem z buildu —
    tím je prompt odpojený od (kešovaného) modelu."""
    from unittest.mock import MagicMock

    import faster_whisper

    import app.post_processor as pp

    fake_model = MagicMock()
    fake_model.transcribe.return_value = ([], object())
    cfg = AppConfig(post_model="large-v3-turbo", language="cs")

    with mock.patch.object(faster_whisper, "WhisperModel", return_value=fake_model):
        transcribe = pp._build_default_transcribe(cfg, attendees=["Petr"])
    # stejná (kešovaná) transcribe fn, ale jiný per-call prompt
    transcribe(object(), initial_prompt="PROMPT PRO TUTO POZNAMKU")

    _, kwargs = fake_model.transcribe.call_args
    assert kwargs["initial_prompt"] == "PROMPT PRO TUTO POZNAMKU"


def test_per_note_prompt_reflects_each_notes_names_not_cached_first(
    tmp_notes_dir, make_meeting, fixed_now, cfg
):
    """KLÍČOVÉ: prompt se počítá per-note z DAT TÉ poznámky, ne z první
    zpracované schůzky. Dvě poznámky s různými účastníky -> různé prompty,
    přestože model (transcribe fn) je kešovaný napříč úkoly."""
    import pathlib

    # Termín do glossary.txt v cwd (vestavěný slovník neexistuje) — ověříme, že
    # slovník je v obou per-note promptech. Testy běží v izolovaném cwd.
    pathlib.Path("glossary.txt").write_text("Kubernetes\n", encoding="utf-8")

    store = NoteStore(tmp_notes_dir)

    # Dvě schůzky s různými jmény (CN -> attendee_names ve frontmatteru).
    m1 = make_meeting(fixed_now, title="Schůzka A")
    m1.attendee_names = ["Alice Aldová"]
    p1 = store.create_note(m1)
    store.append_segment(p1, 0.0, 2.0, "Živý přepis A.")
    w1 = p1[:-3] + ".wav"
    _write_wav(w1)

    m2 = make_meeting(
        fixed_now + timedelta(hours=2), title="Schůzka B"
    )
    m2.attendee_names = ["Bob Bartoš"]
    p2 = store.create_note(m2)
    store.append_segment(p2, 0.0, 2.0, "Živý přepis B.")
    w2 = p2[:-3] + ".wav"
    _write_wav(w2)

    prompts: list = []

    def factory(attendees=None):
        # JEDEN "model" (kešovaná transcribe fn) napříč úkoly. Zachytí per-call
        # prompt a vrátí dostatečně dlouhý finální přepis (projde M5 guardem).
        def fake_transcribe(audio, initial_prompt=None):
            prompts.append(initial_prompt)
            return [(0.0, 2.0, "Kvalitní finální přepis dost dlouhý.")]

        return fake_transcribe

    events = []
    pp = PostProcessor(
        cfg,
        store,
        transcribe_factory=factory,
        on_event=lambda e, d: events.append((e, d)),
    )
    pp.start()
    pp.enqueue(p1, w1)
    pp.enqueue(p2, w2)
    assert _wait_until(
        lambda: sum(1 for e, _ in events if e == "PŘEPIS HOTOVO") >= 2
    ), "oba úkoly se nedokončily"
    pp.stop()

    assert len(prompts) == 2
    prompt_a, prompt_b = prompts[0], prompts[1]
    # každý prompt obsahuje jména SVÉ poznámky a název SVÉ poznámky
    assert "Alice Aldová" in prompt_a and "Schůzka A" in prompt_a
    assert "Bob Bartoš" in prompt_b and "Schůzka B" in prompt_b
    # a NEobsahuje jméno té druhé schůzky (nezůstal zapečený prompt první)
    assert "Bob Bartoš" not in prompt_a
    assert "Alice Aldová" not in prompt_b
    # slovník (termín z glossary.txt) je v obou
    assert "Kubernetes" in prompt_a and "Kubernetes" in prompt_b


def test_read_note_prompt_data_prefers_names_falls_back_to_emails(
    tmp_notes_dir, make_meeting, fixed_now
):
    """_read_note_prompt_data bere attendee_names; když chybí, padá na e-maily."""
    store = NoteStore(tmp_notes_dir)

    # poznámka s CN jmény
    m1 = make_meeting(fixed_now, title="S CN", attendees=["x@a.cz"])
    m1.attendee_names = ["Jana Nováková"]
    p1 = store.create_note(m1)
    names1, title1 = PostProcessor._read_note_prompt_data(p1)
    assert names1 == ["Jana Nováková"]
    assert title1 == "S CN"

    # stará poznámka bez attendee_names -> fallback na e-maily
    m2 = make_meeting(
        fixed_now + timedelta(hours=1), title="Bez CN", attendees=["karel@a.cz"]
    )
    # nesimulujeme attendee_names (zůstane prázdné [])
    p2 = store.create_note(m2)
    names2, title2 = PostProcessor._read_note_prompt_data(p2)
    assert names2 == ["karel@a.cz"]
    assert title2 == "Bez CN"
