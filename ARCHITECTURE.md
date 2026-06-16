# Meeting Notetaker — Architecture

Bot-free meeting notetaker for Windows. Granola-style: captures system audio locally
(WASAPI loopback + mic), transcribes Czech locally via faster-whisper, saves Markdown
notes to disk (readable by Claude via mounted folder). Google Calendar via secret ICS URL.

Target machine: Dell Latitude 7450, Core Ultra 7 165U (CPU-only), 32 GB RAM, Python 3.14, Windows 11.

## Repo layout

```
meeting-notetaker/
  app/
    __init__.py
    config.py          # AppConfig dataclass + load/save JSON
    models.py          # Meeting dataclass, RecorderState enum
    calendar_ics.py    # ICS fetch + parse -> list[Meeting]
    scheduler.py       # decides when to auto start/stop recording
    storage.py         # markdown note files + transcript appending
    audio_capture.py   # WASAPI loopback + mic capture (lib: soundcard)
    transcriber.py     # faster-whisper wrapper, chunk queue -> segments
    recorder.py        # orchestrates capture -> transcribe -> storage, state machine
    ui/
      __init__.py
      main_window.py   # PySide6 main window + tray
      meeting_list.py  # left panel: today + upcoming meetings
      call_panel.py    # right panel: current call status + live transcript
    main.py            # entrypoint: wiring, QApplication
  tests/
    conftest.py        # mocks soundcard & faster_whisper via sys.modules
    test_calendar.py
    test_scheduler.py
    test_storage.py
    test_recorder.py
  notes/               # output .md files (gitignored content)
  models/              # whisper model cache (gitignored)
  config.json          # created on first run
  requirements.txt
  run.bat              # venv python -m app.main
  README.md
```

## Contracts (exact signatures — all modules code against these)

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
    ARMED = "armed"          # meeting starts within arm_window
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
    @property
    def slug(self) -> str: ...   # "2026-06-12_1330_nazev-meetingu" (ascii, max 60)
```

### config.py

```python
@dataclass
class AppConfig:
    ics_url: str = ""
    language: str = "cs"
    live_model: str = "small"        # faster-whisper model name
    post_model: str = ""             # optional re-transcribe after meeting ("" = off)
    notes_dir: str = "notes"         # relative to app root or absolute
    poll_minutes: int = 5            # ICS refresh
    arm_window_s: int = 120          # arm N seconds before start
    stop_grace_s: int = 300          # keep recording N seconds past scheduled end
    chunk_seconds: int = 20          # audio chunk for live transcription
    sample_rate: int = 16000

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
class CalendarService:  # QObject-free, pure python; UI polls it
    def __init__(self, cfg: AppConfig): ...
    def refresh(self) -> list[Meeting]: ...     # fetch+parse, keeps last good result on network error
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

### storage.py

```python
class NoteStore:
    def __init__(self, notes_dir: str): ...
    def create_note(self, meeting: Meeting) -> str:
        """Create notes/<slug>.md with YAML frontmatter (title,start,end,platform,
        attendees,join_url,status: recording) + '## Přepis' heading. Returns path.
        If file exists (restart), append a '--- pokračování ---' marker instead."""
    def append_segment(self, path: str, t0: float, t1: float, text: str) -> None:
        """Append '[HH:MM:SS] text' line (t = seconds from recording start)."""
    def finalize(self, path: str, duration_s: float) -> None:
        """frontmatter status: done + duration."""
    def list_notes(self) -> list[dict]: ...   # [{path,title,start,status}]
```

### audio_capture.py  (Windows-only; imported lazily so tests run on Linux)

```python
class AudioCapture:
    """Captures default speaker loopback + default mic via `soundcard` lib.
    Two threads; mixes to mono float32 @ cfg.sample_rate (resample via numpy linear interp
    from native rate). Emits chunks of cfg.chunk_seconds to a callback."""
    def __init__(self, cfg: AppConfig, on_chunk: Callable[[np.ndarray, float], None]):
        """on_chunk(samples_mono_f32_16k, chunk_start_offset_seconds)"""
    def start(self) -> None: ...
    def stop(self) -> float: ...   # returns total duration seconds
    @property
    def is_running(self) -> bool: ...
```

### transcriber.py

```python
class Transcriber:
    """Owns faster_whisper.WhisperModel (lazy-load on first use, models/ dir,
    device='cpu', compute_type='int8'). Background worker thread consumes a
    queue.Queue of (samples, offset_s); calls on_segments(list[(t0,t1,text)]).
    language=cfg.language, vad_filter=True, beam_size=1 for live."""
    def __init__(self, cfg: AppConfig, on_segments: Callable[[list[tuple[float,float,str]]], None]): ...
    def submit(self, samples: "np.ndarray", offset_s: float) -> None: ...
    def start(self) -> None: ...
    def stop(self, drain: bool = True) -> None: ...   # process remaining queue then exit
    @property
    def queue_depth(self) -> int: ...
```

### recorder.py

```python
class Recorder:
    """State machine. Dependencies injected (capture_factory, transcriber_factory,
    note_store) -> unit-testable with fakes.
    start(meeting): create note, start transcriber+capture, state=RECORDING.
    on_chunk -> transcriber.submit ; on_segments -> store.append_segment.
    stop(): capture.stop, transcriber.stop(drain=True), store.finalize, state=IDLE.
    Emits state changes + new segments via simple callback lists (UI subscribes)."""
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
```

### UI (PySide6)

- `MainWindow(cfg, calendar_service, recorder)`:
  - Left: `MeetingListWidget` — today + 7 days; row = time, title, platform icon (M/T),
    highlight current/next; red dot on the one being recorded.
  - Right: `CallPanel` — when RECORDING: title, elapsed timer (QTimer 1s), red "● NAHRÁVÁ SE",
    live transcript (QPlainTextEdit, append-only, autoscroll), Stop button.
    When idle: next meeting info + "Nahrát teď" (manual) + countdown to auto-start.
  - Status bar: calendar last refresh / error; transcriber queue depth.
  - Tray icon: state color, double-click restore; close button minimizes to tray, tray menu Quit.
  - First-run dialog: asks for ICS URL, saves config.
- Main loop: QTimer every 5 s -> `pick_action(...)` -> recorder start/stop; QTimer poll_minutes -> calendar refresh (in QThread to avoid UI block).
- All UI text in Czech.

## Threading model

- Qt main thread: UI + scheduler tick (cheap, pure function).
- Capture: 2 daemon threads (loopback, mic) inside AudioCapture.
- Transcriber: 1 worker thread (Whisper CPU-bound; chunk of 20 s transcribes in ~5–15 s
  with `small` int8 on this CPU — keeps up live; queue drains on stop).
- Calendar refresh: QThread worker.
- Note appends happen on transcriber thread — NoteStore must use simple `open(...,'a')`
  per call (atomic enough; single writer).

## Failure handling

- ICS fetch fails -> keep last good list, show error in status bar.
- soundcard device missing -> recorder error state, message in CallPanel.
- Whisper model download (first run) -> status in CallPanel ("Stahuji model…"), capture
  buffers to queue meanwhile.
- App restart mid-meeting -> scheduler sees in-progress meeting -> start() -> NoteStore
  appends continuation marker.

## requirements.txt

```
PySide6
faster-whisper
soundcard
numpy
requests
icalendar
recurring-ical-events
python-dateutil
tzdata
pytest        # dev
```

(If any lacks cp314 wheels at deploy time -> fall back to installing Python 3.12 via winget
and venv on 3.12. Decide at deploy, not in code.)

## Tests (pytest, run on Linux — no audio/whisper imports)

- `conftest.py` injects `sys.modules['soundcard']=MagicMock()` and same for `faster_whisper`
  before app imports; provides fixtures: sample ICS text (single + recurring + Meet + Teams
  + all-day event to ignore), tmp notes dir, freeze-time helper.
- test_calendar: parse single/recurring, platform & URL detection, window filter, sort, tz.
- test_scheduler: arm/start/stop transitions incl. grace, overlap, restart mid-meeting.
- test_storage: create/append/finalize/restart-append; frontmatter valid; slug ascii.
- test_recorder: fakes for capture/transcriber -> full happy path + stop drains queue.

## Integration with Claude

Notes land in `C:\temp\Claude\meeting-notetaker\notes\*.md` — already inside the mounted
folder, so Claude reads them directly. No MCP needed (can be added later).
