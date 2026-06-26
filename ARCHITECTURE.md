# Meeting Notetaker — Architecture

A bot-free meeting notetaker for Windows. It captures system audio locally
(WASAPI loopback + microphone), transcribes Czech speech locally with
faster-whisper, and saves Markdown notes to disk. Meetings are read from
Google Calendar via a secret iCal (ICS) URL.

Reference target machine: Dell Latitude 7450, Core Ultra 7 165U (CPU only),
32 GB RAM, Python 3.14, Windows 11.

## Repository layout

```
meeting-notetaker/
  app/
    __init__.py
    config.py          # AppConfig dataclass + load/save JSON
    models.py          # Meeting dataclass, RecorderState enum
    calendar_ics.py    # ICS fetch + parse -> list[Meeting]
    scheduler.py       # decides when to auto start/stop recording
    storage.py         # Markdown note files + transcript appending
    audio_capture.py   # WASAPI loopback + mic capture (via the `soundcard` lib)
    glossary.py        # editable glossary.txt -> Whisper initial_prompt (names/terms)
    transcriber.py     # faster-whisper wrapper, chunk queue -> segments
    recorder.py        # orchestrates capture -> transcribe -> storage; state machine
    call_detector.py   # detects an active call from microphone usage (registry)
    post_processor.py  # re-transcribes the recording with a higher-quality model
    event_log.py       # human-readable call journal (notes/hovory.log)
    ui/
      __init__.py
      main_window.py   # PySide6 main window + tray
      meeting_list.py  # left panel: today + upcoming meetings
      call_panel.py    # right panel: current call status + live transcript
      theme.py         # shared light/dark theme (follows Windows, indigo accent)
    main.py            # entry point: wiring, QApplication
  tests/
    conftest.py        # mocks `soundcard` & `faster_whisper` via sys.modules
    test_calendar.py
    test_scheduler.py
    test_storage.py
    test_recorder.py
    test_call_detector.py
    test_post_processor.py
    test_glossary.py
    test_audio_capture.py
    test_transcriber.py
    test_config.py
  docs/                # screenshots used by the README
  notes/               # output .md files (content is gitignored)
  models/              # Whisper model cache (gitignored)
  config.json          # created on first run (holds the secret ICS URL; gitignored)
  glossary.txt         # editable transcription glossary, created empty next to config.json (gitignored)
  requirements.txt
  run.bat              # venv python -m app.main
  README.md
```

## Contracts (exact signatures — every module is written against these)

### models.py

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class Platform(str, Enum):
    MEET = "meet"
    TEAMS = "teams"
    OTHER = "other"

class RecorderState(str, Enum):
    IDLE = "idle"
    ARMED = "armed"          # a meeting starts within arm_window
    RECORDING = "recording"
    FINALIZING = "finalizing"

@dataclass
class Meeting:
    uid: str                 # ICS UID + start isoformat (recurrence-safe)
    title: str
    start: datetime          # tz-aware, local tz
    end: datetime
    platform: Platform
    join_url: str | None = None
    attendees: list[str] = field(default_factory=list)
    attendee_names: list[str] = field(default_factory=list)  # display names (CN) from the invite, for the prompt glossary
    description: str = ""                                    # cleaned DESCRIPTION (no URLs/emails/boilerplate); topic terms are extracted from it
    @property
    def slug(self) -> str: ...   # "2026-06-12_1330_meeting-title" (ascii, max 60)
```

### config.py

```python
@dataclass
class AppConfig:
    ics_url: str = ""
    language: str = "cs"
    live_model: str = "small"            # faster-whisper model for the live transcript
    post_model: str = "large-v3-turbo"   # re-transcribe after the meeting ("" = off)
    notes_dir: str = "notes"             # relative to app root, or absolute
    poll_minutes: int = 5                # ICS refresh interval
    arm_window_s: int = 120              # arm N seconds before start
    stop_grace_s: int = 300              # keep recording N seconds past scheduled end
    chunk_seconds: int = 20              # audio chunk for live transcription
    sample_rate: int = 16000
    detect_calls: bool = True            # auto-detect a call from microphone usage
    detect_stop_grace_s: int = 20        # stop a detected recording N s after the mic is released
    early_stop_grace_s: int = 60         # stop a calendar recording N s after the call ends
    no_call_timeout_s: int = 180         # stop a calendar recording if no call ever starts

def load_config(path: str) -> AppConfig: ...
def save_config(cfg: AppConfig, path: str) -> None: ...
```

### calendar_ics.py

```python
def fetch_ics(url: str, timeout: int = 15) -> str: ...          # requests.get
def parse_meetings(ics_text: str, window_days: int = 7) -> list[Meeting]:
    """Expand recurring events (recurring_ical_events) from now-12h to now+window_days.
    Detect platform/join_url from LOCATION, DESCRIPTION, X-GOOGLE-CONFERENCE:
      meet.google.com/* -> MEET ; teams.microsoft.com/l/meetup-join or teams.live.com -> TEAMS.
    Sort by start."""
class CalendarService:  # QObject-free, pure Python; the UI polls it
    def __init__(self, cfg: AppConfig): ...
    def refresh(self) -> list[Meeting]: ...     # fetch+parse; keeps the last good result on a network error
    @property
    def meetings(self) -> list[Meeting]: ...
    @property
    def last_error(self) -> str | None: ...
```

### scheduler.py  (pure logic — fully unit-testable)

```python
def pick_action(now: datetime, meetings: list[Meeting], state: RecorderState,
                current: Meeting | None, cfg: AppConfig) -> tuple[str, Meeting | None]:
    """Returns one of: ("none",None) ("arm",m) ("start",m) ("stop",current).
    - arm: state==IDLE and a Meet/Teams meeting starts within arm_window_s
    - start: state in (IDLE, ARMED) and now >= m.start and now <= m.end+stop_grace
    - stop: state==RECORDING and now > current.end + stop_grace_s
    Overlapping meetings: earliest start wins; never auto-stop early for the next one."""
```

### call_detector.py  (Windows-only; `winreg` imported lazily so it loads on Linux)

```python
def detect_label(entries: list[tuple[str, int]]) -> str | None:
    """Pure logic (testable). From (identifier, last_stop) pairs, return a label
    for the active call, or None. `last_stop == 0` means the app is using the mic
    right now. Watched apps: Teams (packaged + unpackaged), Chrome/Edge/Firefox
    (assumed Google Meet). Teams takes priority over a browser."""
def active_call() -> str | None:
    """Read the microphone ConsentStore from the registry and return a call label
    (e.g. "Teams hovor"), or None. Never raises — detection must not crash the app."""
```

Windows records in the registry (CapabilityAccessManager / ConsentStore) which
applications are currently using the microphone. An entry with
`LastUsedTimeStop == 0` means "using the microphone right now". We watch the apps
that host calls and treat a held microphone as an active call.

### storage.py

```python
class NoteStore:
    def __init__(self, notes_dir: str): ...
    def create_note(self, meeting: Meeting) -> str:
        """Create notes/<slug>.md with YAML frontmatter (title, start, end, platform,
        attendees, join_url, status: recording) and a '## Přepis' heading. Returns the path.
        If the file already exists (restart), append a '--- pokračování ---' marker instead."""
    def append_segment(self, path: str, t0: float, t1: float, text: str) -> None:
        """Append a '[HH:MM:SS] text' line (t = seconds from the start of the recording)."""
    def finalize(self, path: str, duration_s: float) -> None:
        """Set frontmatter status: done + duration."""
    def replace_transcript(self, path: str, segments: list) -> bool:
        """Replace the transcript section with the higher-quality re-transcription
        (used by the post-processor); records the model quality and speaker labels.
        Guard against data loss: if the final transcript is empty or much shorter
        than the existing (live) one, the replacement is skipped and False is
        returned (the caller must then keep the WAV); otherwise returns True."""
    def list_notes(self) -> list[dict]: ...   # [{path, title, start, status}]
    # index.jsonl: index_add / index_mark_final keep a lightweight machine-readable index.
```

The note body and frontmatter use a few Czech literals because they appear in the
output files themselves: the `## Přepis` ("Transcript") heading and the
`--- pokračování ---` ("continuation") restart marker.

### audio_capture.py  (Windows-only; imported lazily so tests run on Linux)

```python
class AudioCapture:
    """Captures the default speaker loopback + the default microphone via the
    `soundcard` lib. Two threads; mixes to mono float32 @ cfg.sample_rate
    (resampled from the native rate with numpy linear interpolation). Emits chunks
    of cfg.chunk_seconds to a callback."""
    def __init__(self, cfg: AppConfig, on_chunk: Callable[[np.ndarray, float], None]):
        """on_chunk(samples_mono_f32_16k, chunk_start_offset_seconds)"""
    def start(self) -> None: ...
    def stop(self) -> float: ...   # returns the total duration in seconds
    @property
    def is_running(self) -> bool: ...
```

The recorder also keeps the raw stereo audio (channel 0 = microphone, channel 1 =
loopback) so the post-processor can attribute speakers later.

### glossary.py

```python
GLOSSARY_FILENAME = "glossary.txt"   # editable glossary next to config.json
def ensure_glossary_file(path: str | None = None) -> str: ...
    # Create glossary.txt EMPTY (header comment only) if missing; returns the path.
def extract_topic_terms(title: str, description: str) -> list[str]: ...
    # Locally and deterministically mine "identifier-ish" terms (tool/product
    # names, acronyms, codes) from a meeting's title+description (precision over recall).
def build_initial_prompt(attendees: list[str] | None = None, title: str | None = None,
                         glossary_path: str | None = None,
                         topic_terms: list[str] | None = None) -> str: ...
    # Per-meeting Whisper initial_prompt.
```

The editable `glossary.txt` is the **single source of truth** for terms — there is
no built-in glossary (the `GLOSSARY_TERMS` constant is intentionally empty), so the
user freely adds and removes terms; changes apply to the next transcription with no
rebuild. The topic is **not** hardcoded: `BASE_PROMPT` is neutral and the real topic
comes from the meeting's calendar title. The prompt is assembled per meeting: the
intro + title + auto-mined `topic_terms` go at the **head** (least trusted, dropped
first under budget), and the glossary + attendee names go at the **end** so they
survive Whisper's tail-keeping truncation (rough ~224-token budget; attendees capped
at `MAX_ATTENDEES`).

### transcriber.py

```python
class Transcriber:
    """Owns a faster_whisper.WhisperModel (lazy-loaded on first use, models/ dir,
    device='cpu', compute_type='int8'). A background worker thread consumes a
    queue.Queue of (samples, offset_s) and calls on_segments(list[(t0,t1,text)]).
    vad_filter=True, beam_size=1 for the live pass. The language is detected once
    (multilingual=False): cfg.language is passed verbatim, "auto"/"" -> language=None.
    Each chunk is transcribed with initial_prompt=build_initial_prompt(attendees,
    title, topic_terms) (the name/term glossary) to improve recognition."""
    def __init__(self, cfg: AppConfig,
                 on_segments: Callable[[list[tuple[float,float,str]]], None],
                 on_error: Callable[[str], None] | None = None,
                 model_factory: Callable[[], object] | None = None,
                 attendees: list[str] | None = None,
                 title: str | None = None,
                 topic_terms: list[str] | None = None): ...
    def submit(self, samples: "np.ndarray", offset_s: float) -> None: ...
    def start(self) -> None: ...
    def stop(self, drain: bool = True) -> None: ...   # process the remaining queue, then exit
    @property
    def queue_depth(self) -> int: ...
```

### recorder.py

```python
class Recorder:
    """State machine. Dependencies are injected (capture_factory, transcriber_factory,
    note_store), so it is unit-testable with fakes.
    start(meeting): create the note, start the transcriber + capture, state=RECORDING.
    on_chunk -> transcriber.submit ; on_segments -> store.append_segment.
    stop(): capture.stop, transcriber.stop(drain=True), store.finalize, state=IDLE.
    Emits state changes + new segments via simple callback lists (the UI subscribes)."""
    def __init__(self, cfg, note_store, capture_factory=None, transcriber_factory=None): ...
    state: RecorderState
    current_meeting: Meeting | None
    note_path: str | None
    elapsed_s: float
    def start(self, meeting: Meeting) -> None: ...
    def stop(self) -> None: ...
    def start_manual(self, title: str = "Ruční záznam") -> None: ...  # synthesizes a Meeting
    on_state_changed: list[Callable[[RecorderState], None]]
    on_segment: list[Callable[[float, float, str], None]]
    on_finished: list[Callable[[str, str], None]]   # (note_path, wav_path) when recording ends
```

### post_processor.py

```python
class PostProcessor:
    """After a meeting ends, re-transcribe the saved WAV with the higher-quality
    cfg.post_model (e.g. 'large-v3-turbo'), replace the transcript section in the note,
    attribute speakers from the stereo channels, and delete the WAV. Runs in a daemon
    thread; the model is created lazily on the first task and reused afterwards."""
    def __init__(self, cfg, note_store, transcribe_factory=None, on_event=None): ...
    def start(self) -> None: ...
    def stop(self, drain: bool = False) -> None: ...
    def enqueue(self, note_path: str, wav_path: str) -> None: ...   # no-op if post_model is ""
    def scan_orphans(self, notes_dir: str) -> int:
        """Find *.wav files that still have a matching .md (interrupted runs) and
        enqueue them; returns the count. Lets a restart finish unfinished work."""
    pending: int          # tasks waiting in the queue
    current: str | None   # the note currently being re-transcribed
    busy: bool
```

Speaker attribution compares per-segment RMS energy of the microphone channel
versus the loopback channel: louder microphone -> "Ivan", louder loopback ->
"Ostatní" ("Others"). Mono recordings (older format) get no speaker label.

### UI (PySide6)

- `MainWindow(cfg, calendar_service, recorder, post_processor=None)`:
  - Left: `MeetingListWidget` — today + 7 days as cards: time, title, platform icon
    (M/T), highlight current/next, red dot on the one being recorded.
  - Right: `CallPanel` — when RECORDING: title, an elapsed timer (QTimer, 1 s), a red
    "● NAHRÁVÁ SE" ("recording") indicator, a live transcript (QPlainTextEdit,
    append-only, autoscroll), and a Stop button. When idle: next-meeting info, a
    "Nahrát teď" ("Record now") button, and a countdown to the auto-start.
  - Status bar: calendar last refresh / error; a "⏳ Dopřepisuji…" ("re-transcribing")
    indicator while the post-processor is busy.
  - Tray icon: colour reflects state (grey idle / blue downloading the model / red
    recording / orange re-transcribing); double-click restores the window; the close
    button minimizes to the tray; the tray menu has Quit.
  - First-run dialog: asks for the secret ICS URL and saves the config.
- `theme.py` applies a shared light/dark theme (it follows the Windows setting and
  uses an indigo accent).
- Main loop: a QTimer every 5 s -> `pick_action(...)` -> recorder start/stop, plus the
  call-detection tick; a QTimer every `poll_minutes` -> calendar refresh (in a QThread
  so the UI never blocks).
- All in-app UI text is in Czech.

## Threading model

- Qt main thread: the UI + the scheduler tick (cheap, a pure function).
- Capture: 2 daemon threads (loopback, microphone) inside AudioCapture.
- Transcriber (live): 1 worker thread. Whisper is CPU-bound; a 20 s chunk transcribes
  in roughly 5–15 s with the `small` int8 model on this CPU, so it keeps up live and
  the queue drains on stop.
- Post-processor: 1 daemon thread that re-transcribes finished recordings.
- Model warm-up: 1 short-lived daemon thread started at app launch that pre-downloads the
  live and post models if missing (`model_warmup.py`), then exits. Defensive: any error is
  logged and never blocks startup or recording.
- Calendar refresh: a QThread worker.
- Note appends happen on the transcriber thread — NoteStore uses a simple
  `open(..., 'a')` per call (atomic enough; single writer).

## Failure handling

- ICS fetch fails -> keep the last good list, show a sanitized error in the status bar
  (the secret calendar URL is never written to the log or the UI).
- Audio device missing or lost mid-call -> capture raises a device error that stops the
  recording and shows a rate-limited message.
- Whisper model download (first run, ~2 GB from Hugging Face) -> the live and post models
  are pre-fetched in the background at startup (`model_warmup.py`, a daemon thread that
  exposes an observable status). While the live model is still downloading the UI shows a
  blue tray icon and a "Stahuji model…" status line, and auto-record is **gated**: the
  scheduler/detector do not call `recorder.start` until the live model is on disk (they
  just retry on the next 5 s tick). A fresh install therefore no longer tries to record
  the first call against a still-downloading `model.bin` and crash — the recording starts
  automatically once the model is ready (the manual "Nahrát teď" button is gated the same
  way). If a recording is somehow forced before a model is available, the failure is shown
  as a human message (not the raw CTranslate2 error). The live queue is bounded (drops the
  oldest chunk under back-pressure) and a model-load failure is reported, not swallowed.
- Model storage is centralized in **`model_store.py`** (W2), the single source of truth for
  paths, readiness, download and updates. Each model lives in its **own real-file directory**
  `models/<name>/` (downloaded via `download_model(output_dir=...)`, which writes real files —
  no Hugging Face `blobs/`/symlink cache), and is loaded **by path** with
  `WhisperModel(models/<name>)`. Why: the packaged (PyInstaller) build of CTranslate2 cannot
  open a *symlinked* `model.bin` from the default HF cache ("Unable to open file 'model.bin'")
  even though the file is present and complete and the dev build + Windows open it fine — so
  the model looks downloaded yet recording crashes. Real per-model dirs delete that bug class
  and halve disk (no blob duplication). `is_ready(name)` checks the real `model.bin` (not a
  symlink, ≥ a size floor) plus the full support-file set, so gating is correct (not just
  "dir non-empty"). `ensure_model` first migrates an existing legacy HF cache into the new
  layout transactionally (copy + size-verify + atomic replace, **never deleting** the only
  copy), else downloads with retry/backoff (flaky network / `WinError 10054`). A single-flight
  lock prevents concurrent downloads. Downloads happen only when a model is **missing** — there
  is no per-startup auto-update; a manual tray action "Zkontrolovat aktualizace modelů" fetches
  a newer version into a temp dir that is applied on next restart (never overwriting a loaded
  model). A model that ultimately fails to download is surfaced (amber tray icon + status line
  + one-time tray notification), not silently swallowed.
- App restart mid-meeting -> the scheduler sees the in-progress meeting -> start() ->
  NoteStore appends a continuation marker. Unfinished re-transcriptions are picked up
  by the post-processor's orphan scan on the next start.
- Call detection or post-processing errors are caught and logged; one bad task must
  never crash the worker or the app.

## requirements.txt

```
# Runtime deps are version-pinned in requirements.txt, with a full requirements.lock
# (pip freeze). Dev/build tools (pytest, pyinstaller) live in requirements-dev.txt.
PySide6  faster-whisper  soundcard  numpy  av
requests  icalendar  recurring-ical-events  python-dateutil  tzdata
```

## Tests (pytest — run on Linux, with no audio/Whisper imports)

`conftest.py` injects `sys.modules['soundcard'] = MagicMock()` (and the same for
`faster_whisper`) before the app is imported, and provides fixtures: sample ICS text
(single + recurring + Meet + Teams + an all-day event to ignore), a temp notes dir,
and a freeze-time helper. The suite has 250+ tests, including:

- `test_calendar`: parse single/recurring events, platform & URL detection, window
  filtering, sort order, time zones.
- `test_scheduler`: arm/start/stop transitions including grace periods, overlap, and
  restart mid-meeting.
- `test_storage`: create/append/finalize/restart-append; valid frontmatter; ascii
  slug; transcript replacement and the index.jsonl helpers.
- `test_recorder`: fakes for capture/transcriber -> full happy path + stop drains the
  queue.
- `test_call_detector`: the pure `detect_label` logic — nothing active, Teams
  (packaged + unpackaged), browsers, priority, case-insensitivity.
- `test_post_processor`: happy path replaces the transcript and deletes the WAV, a
  transcription error keeps the WAV and the worker survives, orphan scan, and stereo
  vs. mono speaker attribution.
- `test_glossary`: empty file creation (header only, no built-ins), runtime add/remove
  and `#`/blank-line handling, topic-term extraction (precision over recall), and the
  per-meeting prompt order/token budget (glossary + names protected at the end).
- `test_audio_capture`: resampling, mono mixing, mic/loopback channel pairing and order,
  clipping, and emit offsets.
- `test_transcriber`: bounded queue drops oldest, drain on stop, model-load failure is
  raised, and one bad chunk does not kill the worker.
- `test_model_store`: `is_ready` (full file set + real size, not a symlink), legacy-cache
  migration without download, download-when-missing, ready-skips-download, pending-update
  apply (swap), and `check_for_updates` (ready / current / offline) — the core W2 logic.
- `test_model_warmup`: the startup runner ensures both missing models, skips ready ones,
  dedups live==post, one model's failure is marked `failed` without stopping the other or
  crashing, and the handle exposes a downloading→finished status (used by the UI to gate
  recording and show the indicator/warning).
- `test_config`: round-trip, corrupt-file backup that preserves `ics_url`, and the
  example file matching the code defaults.

Run them with:

```bat
.venv\Scripts\python.exe -m pytest -q
```

## Integration with Claude

Notes land in `notes/*.md` next to the app. Claude can read them directly, but the
primary integration is the bundled **MCP server** (`app/mcp_server.py`, shipped as
`meeting-notetaker-mcp.exe` by the installer): it exposes the transcripts as read-only
tools (list / search / get / today) plus glossary read/edit, and locates the notes via
`%LOCALAPPDATA%\MeetingNotetaker\app-info.json`. A thin companion skill
(`skill/meeting-notetaker/SKILL.md`) routes meeting questions to those tools and carries
the domain rules (Granola arbitration, live-vs-final quality, privacy, offline fallback).
