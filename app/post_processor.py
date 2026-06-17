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

#: Mezní mezera (s) pro slučování sousedních segmentů STEJNÉHO mluvčího.
#: Whisper u váhavé řeči vysype 1-3 slova na řádek; sousední úryvky téhož
#: mluvčího do sebe slijeme, když je mezi nimi mezera < MERGE_GAP_S. Drženo
#: nízko (1.2 s), ať neslepíme dvě samostatné výpovědi.
MERGE_GAP_S = 1.2


def _norm_text(text: str) -> str:
    """Normalizace pro porovnání duplicit: lowercase + sjednocení mezer."""
    return " ".join((text or "").split()).casefold()


def _merge_segments(segments: "list") -> "list":
    """Sloučí roztříštěné segmenty a zahodí doslovné duplicitní sousedy.

    Vstup i výstup jsou 4-tice ``(t0, t1, text, speaker)`` (jako z
    ``_attribute_speakers``). Pravidla (čistá funkce, snadno testovatelná):

    - Sousední segmenty STEJNÉHO mluvčího se slijí do jednoho, když je mezera
      ``next.start - prev.end < MERGE_GAP_S``: text spojen jednou mezerou,
      ``start`` = první, ``end`` = poslední. Přes různé mluvčí se neslučuje.
    - Segment, jehož text se (case- a whitespace-insensitivně) shoduje s
      textem bezprostředně předcházejícího PONECHANÉHO segmentu, se zahodí
      (doslovně zopakovaný soused, např. 3x "A to ještě jako…").
    """
    out: list = []
    for seg in segments:
        t0, t1, text = seg[0], seg[1], seg[2]
        speaker = seg[3] if len(seg) > 3 else None
        if out:
            p0, p1, ptext, pspeaker = out[-1]
            # Doslovný duplicitní soused -> zahodit (nezáleží na mluvčím).
            if _norm_text(text) == _norm_text(ptext):
                # Posuneme konec, ať se nezahodí časový rozsah opakování.
                if t1 > p1:
                    out[-1] = (p0, t1, ptext, pspeaker)
                continue
            # Stejný mluvčí + malá mezera -> slij do předchozího.
            if speaker == pspeaker and (t0 - p1) < MERGE_GAP_S:
                joined = f"{ptext.strip()} {text.strip()}".strip()
                out[-1] = (p0, max(p1, t1), joined, pspeaker)
                continue
        out.append((t0, t1, text, speaker))
    return out


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


def _build_default_transcribe(cfg, attendees=None):
    """Vytvoří přepisovací funkci nad faster-whisper modelem cfg.post_model.

    Model se postaví JEDNOU a drží se (je velký). ``initial_prompt`` (slovník
    jmen/termínů) se NEpečetí do modelu — předává se PER VOLÁNÍ, takže každá
    poznámka dostane prompt ze SVÝCH dat (jména + název), ne z první zpracované
    schůzky. ``attendees`` je jen fallback prompt, když volající žádný nepředá
    (a kvůli zpětné kompatibilitě se starým podpisem).
    """
    from faster_whisper import WhisperModel  # líný import — na Linuxu/testech mock

    from app.glossary import build_initial_prompt

    model = WhisperModel(
        cfg.post_model,
        device="cpu",
        compute_type="int8",
        download_root="models",
        cpu_threads=max(2, (os.cpu_count() or 8) // 2),
        num_workers=1,
    )
    fallback_prompt = build_initial_prompt(attendees)

    def _transcribe(audio, initial_prompt=None):
        # Jazyk detekujeme JEDNOU (multilingual=False): konkrétní kód z configu
        # předáme natvrdo, "auto"/"" -> language=None (faster-whisper detekuje
        # z ~prvních 30 s a dál ho nepřepíná po segmentech).
        lang = cfg.language if cfg.language not in ("", "auto") else None
        # Per-note prompt (z volání) má přednost; jinak fallback z buildu.
        prompt = initial_prompt if initial_prompt is not None else fallback_prompt
        segments, _info = model.transcribe(
            audio,
            language=lang,
            multilingual=False,  # jeden jazyk pro celé audio (žádná re-detekce)
            vad_filter=True,
            vad_parameters=dict(min_speech_duration_ms=250, max_speech_duration_s=30),
            beam_size=2,
            condition_on_previous_text=True,
            initial_prompt=prompt,  # slovník jmen/termínů pro JAKÝKOLI jazyk
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
            lambda attendees=None: _build_default_transcribe(cfg, attendees)
        )
        self._on_event = on_event or (lambda event, detail: None)
        self._transcribe = None  # vytvoří se při prvním úkolu, drží se dál
        self._queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self._stop_event = threading.Event()
        self._drain = False
        self._thread: "threading.Thread | None" = None
        self._current: str | None = None  # poznámka právě v dopřepisu
        #: Stav stavění finálního modelu pro UI (M9): "" | "downloading" |
        #: "loading" | "ready". Nastavuje worker vlákno při prvním úkolu,
        #: čte UI vlákno přes _update_post_status (čtení str atributu je OK).
        self.model_status: str = ""

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

    def stop(self, drain: bool = False, timeout: float = 10.0) -> None:
        """Nastaví stop flag a počká na worker (join ``timeout`` s).

        drain=False (výchozí): rozdělané úkoly nechá ve frontě — orphan scan
        je dožene po dalším startu aplikace. drain=True: nejdřív dokončí frontu.
        Mrtvé vlákno se nejoinuje. (M8: při ukončení appky s rozdělaným přepisem
        volá main.py drain=True s velkorysým timeoutem.)
        """
        self._drain = drain
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)

    def enqueue(self, note_path: str, wav_path: str) -> None:
        """Přidá úkol do fronty. Když je post-processing vypnutý (cfg.post_model
        == ''), WAV se rovnou smaže — jinak by syrový zvuk zůstal trvale ležet
        vedle poznámky (H2: politika retence WAV nezávisí na post-processingu)."""
        if not self.cfg.post_model:
            self._discard_wav(wav_path)
            return
        self._queue.put((note_path, wav_path))

    @staticmethod
    def _read_note_prompt_data(note_path: str) -> "tuple[list[str], str, list[str]]":
        """Přečte z frontmatteru DAT TÉTO poznámky podklady pro initial_prompt:
        jména účastníků, název schůzky a tematické termíny. Preferuje zobrazovaná
        jména (``attendee_names``, CN z kalendáře); když chybí (staré poznámky),
        padá na ``attendees`` (e-maily). ``topic_terms`` jsou auto-extrahované
        termíny uložené při zápisu poznámky (staré poznámky klíč nemají -> []).
        Chyba/chybějící klíč -> prázdné (přepis nesmí spadnout). Vrací
        ``(jména, název, topic_terms)``.
        """
        from app.storage import _parse_frontmatter

        try:
            with open(note_path, "r", encoding="utf-8") as f:
                meta = _parse_frontmatter(f.read())
        except OSError:
            return [], "", []
        names = meta.get("attendee_names")
        if not (isinstance(names, list) and names):
            names = meta.get("attendees")  # fallback: staré poznámky bez CN
        names_list = [str(a) for a in names] if isinstance(names, list) else []
        title = meta.get("title")
        title_str = title if isinstance(title, str) else ""
        terms = meta.get("topic_terms")
        terms_list = [str(t) for t in terms] if isinstance(terms, list) else []
        return names_list, title_str, terms_list

    @staticmethod
    def _read_attendees(note_path: str) -> "list[str]":
        """Zpětně kompatibilní wrapper: jen jména účastníků pro initial_prompt."""
        names, _title, _terms = PostProcessor._read_note_prompt_data(note_path)
        return names

    @staticmethod
    def _discard_wav(wav_path: str) -> None:
        """Smaže WAV (úklid soukromých dat). Chybu jen zaloguje."""
        try:
            if wav_path and os.path.exists(wav_path):
                os.remove(wav_path)
                log.info("WAV smazán (post-processing vypnutý): %s", wav_path)
        except OSError:
            log.exception("Smazání WAV (vypnutý post-processing) selhalo: %s", wav_path)

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
        # Per-note podklady pro initial_prompt: jména účastníků + název +
        # tematické termíny TÉTO poznámky (ne kešované z první schůzky). Prompt
        # sestavíme níž a předáme do volání transcribe — model zůstává kešovaný.
        names, title, topic_terms = self._read_note_prompt_data(note_path)
        if self._transcribe is None:
            # M9: první úkol staví finální model. Když ještě není v models/,
            # spustí ~GB stahování (~2 GB u large-v3-turbo) — ohlásíme to UI
            # přes model_status, ať appka nevypadá zamrzlá. Vlastní stavění
            # běží tady, v daemon vlákně, takže UI mezitím repaintuje.
            from app.transcriber import model_is_downloaded

            if model_is_downloaded(self.cfg.post_model, "models"):
                self.model_status = "loading"
            else:
                self.model_status = "downloading"
            try:
                # Model se staví JEDNOU a drží se; initial_prompt už do něj
                # NEpečetíme (počítá se per-note níž). Předáme jména této první
                # poznámky jen jako fallback (a kvůli starému podpisu). Fake
                # factories v testech berou 0 argumentů -> fallback (L6).
                try:
                    self._transcribe = self._transcribe_factory(attendees=names)
                except TypeError:
                    self._transcribe = self._transcribe_factory()
            finally:
                self.model_status = "ready"
        # Per-note prompt z DAT TÉTO poznámky (jména + název + čerstvý slovník).
        # Tím je prompt odpojený od kešovaného modelu — druhá schůzka dostane
        # svoje jména, ne ta z první.
        from app.glossary import build_initial_prompt

        prompt = build_initial_prompt(names, title=title, topic_terms=topic_terms)
        # Per-call prompt předáme transcribe fn. Fake transcribe v testech berou
        # jen audio (1 argument) -> fallback bez promptu (L6, zpětná kompat).
        try:
            segments = self._transcribe(mono, initial_prompt=prompt)
        except TypeError:
            segments = self._transcribe(mono)
        labeled = _attribute_speakers(channels, framerate, segments)
        # Slij roztříštěné segmenty téhož mluvčího a zahoď doslovné duplicity
        # (Whisper u váhavé řeči sype 1-3 slova na řádek a občas řádek zopakuje).
        labeled = _merge_segments(labeled)
        replaced = self.note_store.replace_transcript(note_path, labeled)

        if not replaced:
            # M5: finální přepis byl prázdný/výrazně horší — necháváme živý
            # přepis i WAV (nemažeme), ať se data neztratí. WAV zůstává pro
            # případný ruční přepis; orphan scan ho sice znovu zařadí, ale
            # příště se opět přeskočí (ohraničené, neškodné).
            log.warning(
                "Finální přepis %s zahozen (prázdný/kratší než živý) — "
                "ponechávám živý přepis a WAV.",
                name,
            )
            self._on_event("PŘEPIS PRESKOCEN", f"{name} (finální horší než živý)")
            return

        try:
            self.note_store.index_mark_final(note_path)
        except Exception:  # noqa: BLE001 - index je bonus
            log.exception("Aktualizace index.jsonl selhala.")
        # WAV mažeme až po úspěšném nahrazení přepisu (H2 + M5).
        self._discard_wav(wav_path)

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
