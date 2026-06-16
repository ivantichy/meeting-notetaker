"""Finální dopřepsání meetingu kvalitnějším modelem po skončení záznamu.

Po skončení meetingu dopřepíše uložený WAV kvalitnějším modelem
(cfg.post_model, např. 'large-v3-turbo'), nahradí sekci Přepis v poznámce
a WAV smaže (úklid místa). Běží v daemon vlákně.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
import wave

log = logging.getLogger(__name__)


def _load_wav_f32(path: str) -> "tuple":
    """Načte WAV jako float32 [-1, 1]; vrací (channels, framerate, délka_s).

    ``channels`` má shape (n, ch). Stereo z recorderu: kanál 0 = mikrofon
    (Ivan), kanál 1 = loopback (ostatní). Starší mono WAV vrátí (n, 1).
    """
    import numpy as np  # lokální import — testy nezávislé na numpy při importu modulu

    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        raise ValueError(
            f"Nepodporovaná šířka vzorku: {sampwidth} B (očekávám 16bit PCM)"
        )
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    channels = audio.reshape(-1, max(n_channels, 1))
    duration_s = len(channels) / float(framerate or 16000)
    return channels, framerate or 16000, duration_s


def _attribute_speakers(channels, framerate: int, segments: "list") -> "list":
    """Přiřadí segmentům mluvčí podle energie kanálů: víc energie v mikrofonu
    -> 'Ivan', víc v loopbacku -> 'Ostatní'. Mono WAV (starý formát) nebo
    prázdné okno -> bez mluvčího. Vrací 4-tice (t0, t1, text, speaker|None)."""
    import numpy as np

    if channels.ndim != 2 or channels.shape[1] < 2:
        return [(t0, t1, text, None) for t0, t1, text in segments]

    mic, loop = channels[:, 0], channels[:, 1]
    out = []
    for t0, t1, text in segments:
        a = max(0, int(t0 * framerate))
        b = min(len(mic), max(int(t1 * framerate), a + 1))
        if b <= a:
            out.append((t0, t1, text, None))
            continue
        rms_mic = float(np.sqrt(np.mean(mic[a:b] ** 2)))
        rms_loop = float(np.sqrt(np.mean(loop[a:b] ** 2)))
        if rms_mic <= 1e-6 and rms_loop <= 1e-6:
            speaker = None
        else:
            speaker = "Ivan" if rms_mic > rms_loop else "Ostatní"
        out.append((t0, t1, text, speaker))
    return out


def _build_default_transcribe(cfg):
    """Vytvoří přepisovací funkci nad faster-whisper modelem cfg.post_model."""
    from faster_whisper import WhisperModel  # líný import — na Linuxu/testech mock

    model = WhisperModel(
        cfg.post_model,
        device="cpu",
        compute_type="int8",
        download_root="models",
        cpu_threads=max(2, (os.cpu_count() or 8) // 2),
        num_workers=1,
    )

    def _transcribe(audio):
        # "auto" / "" -> autodetekce jazyka (z prvních ~30 s nahrávky)
        lang = cfg.language if cfg.language not in ("", "auto") else None
        segments, _info = model.transcribe(
            audio,
            language=lang,
            multilingual=lang is None,  # střídání jazyků uvnitř jednoho meetingu
            vad_filter=True,
            beam_size=2,
            condition_on_previous_text=True,
            initial_prompt="Přepis českého pracovního meetingu." if lang == "cs" else None,
        )
        return [
            (s.start, s.end, s.text.strip()) for s in segments if s.text.strip()
        ]

    return _transcribe


class PostProcessor:
    """Po skončení meetingu dopřepíše uložený WAV kvalitnějším modelem
    (cfg.post_model), nahradí sekci Přepis v poznámce a WAV smaže.

    Worker běží v daemon vlákně; model se vytváří líně až při prvním úkolu
    a drží se pro další úkoly.
    """

    def __init__(self, cfg, note_store, transcribe_factory=None, on_event=None):
        """transcribe_factory: pro testy — callable () -> (audio -> list[(t0,t1,text)]).
        on_event: callable(event: str, detail: str) -> None — deník hovorů; None = no-op.
        """
        self.cfg = cfg
        self.note_store = note_store
        self._transcribe_factory = transcribe_factory or (
            lambda: _build_default_transcribe(cfg)
        )
        self._on_event = on_event or (lambda event, detail: None)
        self._transcribe = None  # vytvoří se při prvním úkolu, drží se dál
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._drain = False
        self._thread: "threading.Thread | None" = None
        self._current: str | None = None  # poznámka právě v dopřepisu

    def start(self) -> None:
        """Spustí worker vlákno (idempotentní)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._drain = False
        self._thread = threading.Thread(
            target=self._worker, name="post-processor", daemon=True
        )
        self._thread.start()

    def stop(self, drain: bool = False) -> None:
        """Nastaví stop flag a počká na worker (join timeout 10 s).

        drain=False (výchozí): rozdělané úkoly nechá ve frontě — orphan scan
        je dožene po dalším startu aplikace. drain=True: nejdřív dokončí frontu.
        """
        self._drain = drain
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def enqueue(self, note_path: str, wav_path: str) -> None:
        """Přidá úkol do fronty. No-op pokud cfg.post_model je '' (vypnuto)."""
        if not self.cfg.post_model:
            return
        self._queue.put((note_path, wav_path))

    def scan_orphans(self, notes_dir: str) -> int:
        """Najde v notes_dir soubory *.wav, k nimž existuje stejnojmenné .md;
        každý pošle do enqueue(). Vrací počet. WAVy bez .md ignoruje.
        """
        try:
            names = os.listdir(notes_dir)
        except OSError:
            return 0
        count = 0
        for name in sorted(names):
            if not name.lower().endswith(".wav"):
                continue
            md_path = os.path.join(notes_dir, name[:-4] + ".md")
            if not os.path.isfile(md_path):
                continue
            self.enqueue(md_path, os.path.join(notes_dir, name))
            count += 1
        return count

    @property
    def pending(self) -> int:
        """Počet úkolů čekajících ve frontě."""
        return self._queue.qsize()

    @property
    def current(self) -> str | None:
        """Název poznámky, která se právě dopřepisuje (None = nic neběží)."""
        return self._current

    @property
    def busy(self) -> bool:
        """True, pokud se právě dopřepisuje nebo čekají úkoly ve frontě."""
        return self._current is not None or not self._queue.empty()

    # ------------------------------------------------------------------ worker

    def _worker(self) -> None:
        while True:
            if self._stop_event.is_set() and not (
                self._drain and not self._queue.empty()
            ):
                break
            try:
                note_path, wav_path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._current = os.path.basename(note_path)
            try:
                self._process(note_path, wav_path)
            except Exception as exc:  # jeden vadný úkol nesmí zabít worker
                log.exception("Finální přepis selhal: %s", note_path)
                self._on_event(
                    "PŘEPIS CHYBA", f"{os.path.basename(note_path)}: {exc}"
                )
            finally:
                self._current = None
                self._queue.task_done()

    def _process(self, note_path: str, wav_path: str) -> None:
        name = os.path.basename(note_path)
        log.info("Finální přepis startuje: %s (%s)", name, wav_path)
        self._on_event("PŘEPIS START", name)
        t_start = time.monotonic()

        import numpy as np

        channels, framerate, duration_s = _load_wav_f32(wav_path)
        mono = np.clip(channels.mean(axis=1), -1.0, 1.0).astype(np.float32)
        if self._transcribe is None:
            self._transcribe = self._transcribe_factory()
        segments = self._transcribe(mono)
        labeled = _attribute_speakers(channels, framerate, segments)
        self.note_store.replace_transcript(note_path, labeled)
        try:
            self.note_store.index_mark_final(note_path)
        except Exception:  # noqa: BLE001 - index je bonus
            log.exception("Aktualizace index.jsonl selhala.")
        os.remove(wav_path)

        elapsed = time.monotonic() - t_start
        log.info(
            "Finální přepis hotov: %s (%.0f s audia za %.0f s, %d segmentů)",
            name,
            duration_s,
            elapsed,
            len(segments),
        )
        self._on_event(
            "PŘEPIS HOTOVO", f"{name} ({duration_s:.0f}s audia za {elapsed:.0f}s)"
        )
