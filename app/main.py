"""Vstupní bod aplikace Meeting Notetaker."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


def _setup_logging() -> None:
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers = [
        logging.FileHandler("notetaker.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ]
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=handlers)
    logging.captureWarnings(True)  # warnings (např. soundcard) -> notetaker.log


def main() -> int:
    # Kořen aplikace = nadřazený adresář balíčku app/
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    _setup_logging()
    log = logging.getLogger(__name__)
    log.info("Spouštím Meeting Notetaker (kořen: %s)", root)

    from PySide6.QtWidgets import QApplication, QInputDialog, QLineEdit

    from app.calendar_ics import CalendarService
    from app.config import load_config, save_config
    from app.recorder import Recorder
    from app.storage import NoteStore
    from app.ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Meeting Notetaker")
    app.setQuitOnLastWindowClosed(False)  # okno se zavírá do oznamovací oblasti

    from app.ui.theme import apply_theme

    apply_theme(app)  # jednotný vzhled (světlý/tmavý dle Windows, indigo akcent)

    cfg = load_config("config.json")

    if not cfg.ics_url:
        url, ok = QInputDialog.getText(
            None,
            "Tajná ICS adresa Google Kalendáře",
            "Vložte tajnou ICS adresu svého Google Kalendáře.\n\n"
            "Najdete ji v Google Kalendáři: Nastavení → Nastavení mého kalendáře\n"
            "→ Integrovat kalendář → „Tajná adresa ve formátu iCal“.",
            QLineEdit.EchoMode.Normal,
            "",
        )
        if ok and url.strip():
            cfg.ics_url = url.strip()
        save_config(cfg, "config.json")

    note_store = NoteStore(cfg.notes_dir)
    calendar_service = CalendarService(cfg)
    recorder = Recorder(cfg, note_store)

    # Deník hovorů (lidsky čitelný log start/stop/přepis).
    from app.event_log import EventLog

    events = EventLog(os.path.join(cfg.notes_dir, "hovory.log"))

    def _log_state(state):
        m = recorder.current_meeting
        if str(getattr(state, "value", state)) == "recording" and m is not None:
            events.log("ZÁZNAM START", f"{m.title} ({m.platform.value})")

    recorder.on_state_changed.append(_log_state)
    recorder.on_finished.append(
        lambda note, wav: events.log("ZÁZNAM KONEC", os.path.basename(note))
    )

    # Dvoufázový přepis: po meetingu dopřepsat WAV kvalitnějším modelem.
    from app.post_processor import PostProcessor

    post_processor = PostProcessor(cfg, note_store, on_event=events.log)
    if cfg.post_model:
        post_processor.start()
        orphans = post_processor.scan_orphans(cfg.notes_dir)
        if orphans:
            log.info("Zařazeno %d nedopřepsaných WAV z minula.", orphans)
    recorder.on_finished.append(post_processor.enqueue)

    window = MainWindow(cfg, calendar_service, recorder, post_processor=post_processor)
    window.show()
    rc = app.exec()
    post_processor.stop(drain=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
