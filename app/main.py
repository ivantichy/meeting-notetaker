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

    import tempfile

    from PySide6.QtCore import QLockFile
    from PySide6.QtWidgets import QApplication, QMessageBox

    from app.calendar_ics import CalendarService
    from app.config import load_config, save_config
    from app.recorder import Recorder
    from app.storage import NoteStore
    from app.ui.main_window import MainWindow
    from app.ui.onboarding import IcsSetupDialog

    app = QApplication(sys.argv)
    app.setApplicationName("Meeting Notetaker")
    app.setQuitOnLastWindowClosed(False)  # okno se zavírá do oznamovací oblasti

    # --- jediná instance ----------------------------------------------------
    # Zámek brání spuštění druhé instance (a tím dvojímu nahrávání téhož hovoru).
    # Umístíme ho do uživatelského %LOCALAPPDATA% (per-user, nesdílený) místo do
    # společného temp — tam by ho na multi-user stroji mohl předem obsadit někdo
    # jiný (predikovatelné jméno) a zablokovat spuštění (L2). Fallback na temp.
    lock_dir = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    lock_dir = os.path.join(lock_dir, "MeetingNotetaker")
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError:
        lock_dir = tempfile.gettempdir()
    lock_path = os.path.join(lock_dir, "meeting-notetaker.lock")
    lock_file = QLockFile(lock_path)
    lock_file.setStaleLockTime(0)  # zámek po pádu uvolní OS automaticky
    if not lock_file.tryLock(100):
        log.warning("Druhá instance — aplikace už běží, končím.")
        QMessageBox.information(
            None,
            "Meeting Notetaker",
            "Meeting Notetaker už běží.",
        )
        return 0
    app._single_instance_lock = lock_file  # udržet referenci po dobu běhu

    from app.ui.theme import apply_theme

    apply_theme(app)  # jednotný vzhled (světlý/tmavý dle Windows, indigo akcent)

    cfg = load_config("config.json")

    # Locator: zveřejni na PEVNÉ cestě (%LOCALAPPDATA%\MeetingNotetaker\
    # app-info.json), kde leží poznámky tohoto běhu — ať skilly/tasky najdou
    # přepisy bez ohledu na dev vs. nainstalovaný build. Plně defenzivní:
    # selhání nikdy neshodí start.
    from app.app_info import write_app_info

    write_app_info(cfg)

    if not cfg.ics_url:
        url = IcsSetupDialog.get_url(None, initial="")
        if url:
            cfg.ics_url = url
        # Uložíme i při zrušení (zachová dosavadní chování: aplikace se
        # spustí s prázdným kalendářem a adresu lze doplnit přes „Nastavení…“).
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

    # W1: hned po startu na pozadí předstáhni modely (živý i finální), ať čerstvá
    # instalace s prázdnou cache models/ nezabije první hovor stahováním přesně v
    # okamžiku, kdy má začít nahrávání. Neblokuje UI a selhání je nezávazné
    # (nahrávání má svůj dosavadní lazy fallback).
    from app.model_warmup import start_model_warmup

    warmup = start_model_warmup(cfg)

    window = MainWindow(
        cfg,
        calendar_service,
        recorder,
        post_processor=post_processor,
        model_warmup=warmup,
    )
    # Start na pozadí: aplikace žije v oznamovací oblasti (tray). Okno otevře
    # uživatel dvojklikem na ikonu nebo přes „Zobrazit" v jejím menu.
    from PySide6.QtWidgets import QSystemTrayIcon

    window._tray.showMessage(
        "Meeting Notetaker",
        "Běží na pozadí. Dvojklikem na ikonu otevřete okno.",
        QSystemTrayIcon.MessageIcon.Information,
        4000,
    )
    rc = app.exec()
    # M8: při ukončení dokonči rozdělaný/zafrontovaný dopřepis (drain=True) s
    # velkorysým timeoutem — jinak by se hotové meetingy čekající na kvalitní
    # přepis zahodily (WAV by sice zůstal a orphan scan by je dohnal příště,
    # ale jen pokud uživatel appku znovu spustí). Co se nestihne, zůstává na
    # disku pro příští běh.
    if cfg.post_model and post_processor.busy:
        log.info("Ukončuji: dokončuji rozdělaný dopřepis (drain).")
        post_processor.stop(drain=True, timeout=300.0)
    else:
        post_processor.stop(drain=False)
    return rc


if __name__ == "__main__":
    sys.exit(main())
