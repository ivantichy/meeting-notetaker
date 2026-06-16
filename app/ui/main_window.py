"""Hlavní okno aplikace Meeting Notetaker + ikona v oznamovací oblasti."""

from __future__ import annotations

import logging
from datetime import datetime

from dateutil.tz import tzlocal

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSplitter,
    QSystemTrayIcon,
)

from app.models import RecorderState
from app.scheduler import evaluate_calendar_call, gate_start_on_call, pick_action
from app.ui.call_panel import CallPanel
from app.ui.meeting_list import MeetingListWidget
from app.ui.onboarding import IcsSetupDialog
from app.ui.theme import STATUS_ERROR, STATUS_PROCESSING

log = logging.getLogger(__name__)

_STATE_NAMES_CZ = {
    RecorderState.IDLE: "Nečinný",
    RecorderState.ARMED: "Připraven k záznamu",
    RecorderState.RECORDING: "Nahrává se",
    RecorderState.FINALIZING: "Dokončuji přepis",
}


class _Bridge(QObject):
    """Most pro callbacky recorderu (volané z pracovních vláken) do UI vlákna."""

    state_changed = Signal(str)
    segment = Signal(float, float, str)
    device_error = Signal(str)  # výpadek zvukového zařízení během záznamu (H5)


class _CalendarWorker(QThread):
    """Aktualizace kalendáře na pozadí, aby se neblokovalo UI."""

    done = Signal()

    def __init__(self, service, parent=None) -> None:
        super().__init__(parent)
        self._service = service

    def run(self) -> None:
        try:
            self._service.refresh()
        except Exception:  # noqa: BLE001
            log.exception("Aktualizace kalendáře selhala")
        self.done.emit()


def _make_tray_icon(color: str) -> QIcon:
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(4, 4, 24, 24)
    painter.end()
    return QIcon(pixmap)


class MainWindow(QMainWindow):
    def __init__(
        self, cfg, calendar_service, recorder, post_processor=None, parent=None
    ) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._calendar = calendar_service
        self._recorder = recorder
        self._post_processor = post_processor
        self._calendar_worker: _CalendarWorker | None = None
        self._tray_message_shown = False
        self._quitting = False
        self._armed_uid: str | None = None
        self._detector_uid: str | None = None
        self._detector_last_seen: float = 0.0
        self._detector_cooldown_until: float = 0.0
        self._call_seen_in_recording: bool = False
        self._call_last_seen: float = 0.0
        self._last_recording_uid: str | None = None
        #: uid -> monotonic čas, do kdy daný meeting znovu nespouštět (cooldown).
        #: Brání restartu záznamu každých 5 s až do konce události, ale po
        #: vypršení cooldownu umožní pozdní příchod / reconnect znovu nahrát (H7).
        self._stopped_until: dict[str, float] = {}
        #: Po jak dlouho po zastavení daný uid znovu nearmovat (s).
        self._restart_cooldown_s: float = 600.0
        #: poslední chyba zařízení zobrazená uživateli (rate-limit, L4)
        self._last_device_error_ts: float = 0.0

        self.setWindowTitle("Meeting Notetaker")
        self.resize(1000, 640)

        # --- rozložení -----------------------------------------------------
        self._meeting_list = MeetingListWidget()
        self._call_panel = CallPanel(cfg, recorder)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._meeting_list)
        splitter.addWidget(self._call_panel)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 680])
        self.setCentralWidget(splitter)

        self.statusBar().showMessage("Načítám kalendář…")
        self._post_label = QLabel("")
        self._post_label.setStyleSheet(
            f"color: {STATUS_PROCESSING}; padding-right: 6px;"
        )
        self.statusBar().addPermanentWidget(self._post_label)

        # --- horní lišta -----------------------------------------------------
        toolbar = self.addToolBar("Hlavní")
        toolbar.setMovable(False)
        self._refresh_action = QAction("⟳ Obnovit kalendář", self)
        self._refresh_action.triggered.connect(self._on_refresh_clicked)
        toolbar.addAction(self._refresh_action)
        toolbar.addSeparator()
        self._record_action = QAction("● Nahrát teď", self)
        self._record_action.triggered.connect(self._call_panel.trigger_record_now)
        toolbar.addAction(self._record_action)

        self._meeting_list.meeting_selected.connect(self._on_meeting_selected)

        # --- callbacky recorderu -> UI vlákno -------------------------------
        self._bridge = _Bridge(self)
        self._bridge.state_changed.connect(
            self._on_state_changed, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.segment.connect(
            self._on_segment, Qt.ConnectionType.QueuedConnection
        )
        self._bridge.device_error.connect(
            self._on_device_error, Qt.ConnectionType.QueuedConnection
        )
        recorder.on_state_changed.append(
            lambda s: self._bridge.state_changed.emit(getattr(s, "value", str(s)))
        )
        recorder.on_segment.append(
            lambda t0, t1, text: self._bridge.segment.emit(float(t0), float(t1), text)
        )
        # H5: výpadek zařízení (z capture vlákna) marshalovat do UI vlákna.
        recorder.on_device_error.append(
            lambda msg: self._bridge.device_error.emit(str(msg))
        )

        # --- tray ------------------------------------------------------------
        self._icon_idle = _make_tray_icon("#9e9e9e")
        self._icon_recording = _make_tray_icon("#e53935")
        self._icon_postprocessing = _make_tray_icon("#fb8c00")  # oranžová: dopřepis WAV
        self.setWindowIcon(self._icon_idle)

        self._tray = QSystemTrayIcon(self._icon_idle, self)
        self._tray.setToolTip(f"Meeting Notetaker — {_STATE_NAMES_CZ[RecorderState.IDLE]}")
        tray_menu = QMenu(self)
        show_action = QAction("Zobrazit", self)
        show_action.triggered.connect(self._restore_window)
        settings_action = QAction("Nastavení…", self)
        settings_action.triggered.connect(self._open_settings)
        quit_action = QAction("Ukončit", self)
        quit_action.triggered.connect(self._quit)
        tray_menu.addAction(show_action)
        tray_menu.addAction(settings_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self._tray.setContextMenu(tray_menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

        # --- časovače --------------------------------------------------------
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(max(cfg.poll_minutes, 1) * 60 * 1000)
        self._refresh_timer.timeout.connect(self.refresh_calendar)
        self._refresh_timer.start()

        self._scheduler_timer = QTimer(self)
        self._scheduler_timer.setInterval(5000)
        self._scheduler_timer.timeout.connect(self._scheduler_tick)
        self._scheduler_timer.start()

        self._post_timer = QTimer(self)
        self._post_timer.setInterval(3000)
        self._post_timer.timeout.connect(self._update_post_status)
        self._post_timer.start()

        QTimer.singleShot(0, self.refresh_calendar)

    # ------------------------------------------------------------- kalendář

    def _on_refresh_clicked(self) -> None:
        self.statusBar().setStyleSheet("")
        self.statusBar().showMessage("Aktualizuji kalendář…")
        self.refresh_calendar()

    def refresh_calendar(self) -> None:
        if self._calendar_worker is not None:
            return  # refresh už běží
        worker = _CalendarWorker(self._calendar, self)
        worker.done.connect(self._on_calendar_refreshed, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(
            self._on_calendar_worker_finished, Qt.ConnectionType.QueuedConnection
        )
        self._calendar_worker = worker
        worker.start()

    def _on_calendar_worker_finished(self) -> None:
        worker, self._calendar_worker = self._calendar_worker, None
        if worker is not None:
            worker.deleteLater()

    def _on_calendar_refreshed(self) -> None:
        err = self._calendar.last_error
        if err:
            self.statusBar().setStyleSheet(f"color: {STATUS_ERROR};")
            self.statusBar().showMessage(f"Kalendář: chyba — {err}")
        else:
            self.statusBar().setStyleSheet("")
            now = datetime.now(tz=tzlocal())
            self.statusBar().showMessage(
                f"Kalendář aktualizován {now.strftime('%H:%M:%S')}"
            )
        self._update_meeting_list()
        self._update_next_meeting_info()

    # ------------------------------------------------------------- plánovač

    def _current_call_label(self) -> "str | None":
        """Aktivní hovor podle mikrofonu — spočítá se JEDNOU za tick a předává
        se dál (M7: dřív se rekurzivní čtení registru volalo až 3× za tick na
        UI vlákně). Bez zapnuté detekce vrací None bez sahání do registru."""
        if not getattr(self._cfg, "detect_calls", True):
            return None
        from app.call_detector import active_call

        return active_call()

    def _scheduler_tick(self) -> None:
        now = datetime.now(tz=tzlocal())
        # M7: jedno čtení stavu mikrofonu na celý tick (sdílí start/stop/detektor).
        call_label = self._current_call_label()
        try:
            action, meeting = pick_action(
                now,
                self._calendar.meetings,
                self._recorder.state,
                self._recorder.current_meeting,
                self._cfg,
            )
        except Exception:  # noqa: BLE001
            log.exception("pick_action selhal")
            return

        # Meeting, který už jednou nahrával a byl zastaven, po dobu cooldownu
        # znovu nespouštět — jinak plánovač restartuje záznam každých 5 s až do
        # konce události. Po vypršení cooldownu (H7) se ale smí znovu armovat,
        # takže pozdní příchod / reconnect na kalendářovou schůzku se nahraje.
        if action in ("arm", "start") and meeting is not None:
            import time as _t

            until = self._stopped_until.get(meeting.uid)
            if until is not None:
                if _t.monotonic() < until:
                    action = "none"
                else:
                    del self._stopped_until[meeting.uid]  # cooldown vypršel

        if action == "arm":
            self._armed_uid = meeting.uid if meeting else None
            self._meeting_list.set_armed_uid(self._armed_uid)
            self._call_panel.set_next_meeting(meeting, armed=True)
        elif action == "start":
            # Nezačínat "naslepo" v čase události: počkat, až se hovor
            # skutečně rozběhne (Teams/prohlížeč drží mikrofon). Bez toho
            # vznikaly prázdné záznamy meetingů, na které se Ivan nepřipojil.
            # Rozhodnutí je čistá funkce (H7); stav mikrofonu z cache (M7).
            if gate_start_on_call(
                getattr(self._cfg, "detect_calls", True), call_label is not None
            ):
                self._armed_uid = meeting.uid if meeting else None
                self._meeting_list.set_armed_uid(self._armed_uid)
                self._call_panel.set_next_meeting(meeting, armed=True)
                return
            self._armed_uid = None
            self._meeting_list.set_armed_uid(None)
            try:
                self._recorder.start(meeting)
            except Exception as exc:  # noqa: BLE001
                log.exception("Automatický start záznamu selhal")
                QMessageBox.critical(
                    self,
                    "Chyba záznamu",
                    f"Nepodařilo se spustit záznam schůzky „{meeting.title}“:\n{exc}",
                )
        elif action == "stop":
            # Kalendářový čas vypršel, ale meeting se může protáhnout:
            # dokud hovor reálně běží (mikrofon v držení Teams/prohlížeče),
            # nahráváme dál. Zastavíme až po uvolnění mikrofonu.
            if call_label is not None:
                return
            self._call_panel.request_stop()
        else:
            if self._armed_uid is not None and self._recorder.state == RecorderState.IDLE:
                self._armed_uid = None
                self._meeting_list.set_armed_uid(None)
            self._update_next_meeting_info()
            self._detector_tick(now, call_label)

    def _detector_tick(self, now: datetime, label: "str | None") -> None:
        """Auto-detekce hovoru mimo kalendář: aplikace (Teams/prohlížeč)
        právě používá mikrofon -> spustit záznam; po uvolnění mikrofonu
        (+ grace) záznam zastavit. Záznamy řízené kalendářem nezastavuje.

        ``label`` je aktivní hovor spočítaný jednou v ``_scheduler_tick`` (M7).
        Vlastní rozhodovací logika u kalendářového záznamu je čistá funkce
        ``evaluate_calendar_call`` ve scheduleru (H7)."""
        if not getattr(self._cfg, "detect_calls", True):
            return
        import time as _time

        state = self._recorder.state

        if state == RecorderState.RECORDING:
            current = self._recorder.current_meeting
            if current is None:
                return
            if current.uid != self._detector_uid:
                # Záznam řízený kalendářem — osud podle aktivity hovoru řeší
                # čistá, testovaná funkce evaluate_calendar_call (H7).
                from app.models import Platform

                if current.platform not in (Platform.MEET, Platform.TEAMS):
                    return  # ruční záznamy nezastavujeme
                if label:
                    self._call_last_seen = _time.monotonic()
                decision, self._call_seen_in_recording = evaluate_calendar_call(
                    call_active=label is not None,
                    call_seen=self._call_seen_in_recording,
                    secs_since_last_call=_time.monotonic() - self._call_last_seen,
                    elapsed_s=self._recorder.elapsed_s,
                    early_stop_grace_s=getattr(self._cfg, "early_stop_grace_s", 60),
                    no_call_timeout_s=getattr(self._cfg, "no_call_timeout_s", 180),
                )
                if decision == "stop_early":
                    log.info(
                        "Hovor skončil dřív než kalendářová událost — zastavuji záznam."
                    )
                    self._call_panel.request_stop()
                elif decision == "stop_no_call":
                    log.info(
                        "Žádný hovor se nerozběhl do %d s — zastavuji záznam "
                        "(uživatel se k meetingu nepřipojil).",
                        getattr(self._cfg, "no_call_timeout_s", 180),
                    )
                    self._call_panel.request_stop()
                return
            if label:
                self._detector_last_seen = _time.monotonic()
            elif (
                _time.monotonic() - self._detector_last_seen
                > getattr(self._cfg, "detect_stop_grace_s", 20)
            ):
                log.info("Mikrofon uvolněn — zastavuji detekovaný záznam.")
                self._detector_uid = None
                self._detector_cooldown_until = _time.monotonic() + 60
                self._call_panel.request_stop()
            return

        if state == RecorderState.IDLE and self._detector_uid is not None:
            # Detekovaný záznam skončil jinak (ruční stop) — cooldown,
            # ať se okamžitě nespustí znovu, dokud je mikrofon aktivní.
            self._detector_uid = None
            self._detector_cooldown_until = _time.monotonic() + 60

        if (
            label
            and state == RecorderState.IDLE
            and _time.monotonic() >= self._detector_cooldown_until
        ):
            log.info("Detekován hovor (%s) — spouštím záznam.", label)
            try:
                self._recorder.start_manual(title=label)
                current = self._recorder.current_meeting
                self._detector_uid = current.uid if current else None
                self._detector_last_seen = _time.monotonic()
            except Exception:  # noqa: BLE001
                log.exception("Start detekovaného záznamu selhal")
                self._detector_cooldown_until = _time.monotonic() + 120

    def _model_download_message(self) -> str:
        """Hláška o jednorázovém stahování Whisper modelu (M9), nebo "".

        Při prvním spuštění stáhne faster-whisper model (~2 GB u finálního),
        což trvá minuty — bez indikátoru by appka vypadala zamrzlá. Stav čteme
        z post-processoru (běží v daemon vlákně, takže UI mezitím repaintuje)
        i z živého přepisu (transcriber), pokud zrovna existuje. Finální model
        má přednost (je výrazně větší). Stav je jen str atribut — čtení napříč
        vlákny je bezpečné (widgety zde nikdo z worker vlákna nesahá)."""
        pp = self._post_processor
        if pp is not None and getattr(pp, "model_status", "") == "downloading":
            return "⏳ Stahuji model přepisu… (jednorázově, ~2 GB)"
        # Živý model staví recorder ve svém transcriberu; čteme ho best-effort.
        live = getattr(self._recorder, "_transcriber", None)
        if live is not None and getattr(live, "model_status", "") == "downloading":
            return "⏳ Stahuji model živého přepisu… (jednorázově)"
        return ""

    def _update_post_status(self) -> None:
        """Indikátor dopřepisování WAV + stahování modelu: text ve stavové
        liště + oranžová tray ikona (pokud zrovna nenahráváme — červená má
        přednost)."""
        pp = self._post_processor
        if pp is None:
            return
        downloading = self._model_download_message()
        busy = pp.busy
        if downloading:
            # Stahování modelu má přednost před hláškou o dopřepisu (M9).
            self._post_label.setText(downloading)
        elif busy:
            current = pp.current
            waiting = pp.pending
            text = f"⏳ Dopřepisuji: {current or '…'}"
            if waiting:
                text += f" (+{waiting} ve frontě)"
            self._post_label.setText(text)
        else:
            self._post_label.setText("")

        recording = self._recorder.state in (
            RecorderState.RECORDING,
            RecorderState.FINALIZING,
        )
        if recording:
            return  # červenou ikonu řídí _on_state_changed
        if busy or downloading:
            self._tray.setIcon(self._icon_postprocessing)
            self.setWindowIcon(self._icon_postprocessing)
            tip = (
                "Meeting Notetaker — stahuji model"
                if downloading
                else "Meeting Notetaker — dopřepisuji záznam"
            )
            self._tray.setToolTip(tip)
        else:
            self._tray.setIcon(self._icon_idle)
            self.setWindowIcon(self._icon_idle)
            self._tray.setToolTip(
                f"Meeting Notetaker — {_STATE_NAMES_CZ[RecorderState.IDLE]}"
            )

    def _update_next_meeting_info(self) -> None:
        if self._armed_uid is not None:
            return  # informaci právě řídí stav "arm"
        now = datetime.now(tz=tzlocal())
        next_meeting = None
        for m in self._calendar.meetings:
            if m.start >= now:
                next_meeting = m
                break
        self._call_panel.set_next_meeting(next_meeting, armed=False)

    # ------------------------------------------------- callbacky recorderu

    def _on_state_changed(self, value: str) -> None:
        try:
            state = RecorderState(value)
        except ValueError:
            log.warning("Neznámý stav recorderu: %r", value)
            return
        self._call_panel.set_state(state)
        self._record_action.setEnabled(
            state in (RecorderState.IDLE, RecorderState.ARMED)
        )
        if state == RecorderState.RECORDING:
            self._armed_uid = None
            self._meeting_list.set_armed_uid(None)
            self._call_seen_in_recording = False
            self._call_last_seen = 0.0
            current = self._recorder.current_meeting
            self._last_recording_uid = current.uid if current else None
        elif state == RecorderState.IDLE and self._last_recording_uid:
            import time as _t

            # Cooldown místo trvalého bloku (H7): po vypršení se smí znovu
            # armovat (pozdní příchod / reconnect). Manuální/detekované záznamy
            # mají uid "manual:…" a do plánovače nevstupují, takže neškodí.
            self._stopped_until[self._last_recording_uid] = (
                _t.monotonic() + self._restart_cooldown_s
            )
            self._last_recording_uid = None
        self._update_meeting_list()

        if state == RecorderState.RECORDING:
            self._tray.setIcon(self._icon_recording)
            self.setWindowIcon(self._icon_recording)
        else:
            self._tray.setIcon(self._icon_idle)
            self.setWindowIcon(self._icon_idle)
        name = _STATE_NAMES_CZ.get(state, str(state))
        self._tray.setToolTip(f"Meeting Notetaker — {name}")

    def _on_segment(self, t0: float, t1: float, text: str) -> None:
        self._call_panel.append_segment(t0, t1, text)

    def _on_device_error(self, message: str) -> None:
        """Výpadek/odpojení zvukového zařízení uprostřed záznamu (H5): zastavit
        záznam a upozornit uživatele. Hláška je rate-limitovaná (L4), ať při
        trvalé chybě nespamuje modály; do status baru píšeme vždy."""
        import time as _t

        log.warning("Výpadek zvukového zařízení během záznamu: %s", message)
        self.statusBar().setStyleSheet(f"color: {STATUS_ERROR};")
        self.statusBar().showMessage("Záznam přerušen — výpadek zvukového zařízení.")
        if self._recorder.state == RecorderState.RECORDING:
            self._call_panel.request_stop()
        now = _t.monotonic()
        if now - self._last_device_error_ts > 30.0:
            self._last_device_error_ts = now
            QMessageBox.warning(
                self,
                "Chyba zvukového zařízení",
                "Záznam byl přerušen, protože zvukové zařízení přestalo být "
                "dostupné (např. odpojení sluchátek nebo změna výchozího "
                "zařízení).\n\nDosavadní část záznamu je uložena.",
            )

    def _update_meeting_list(self) -> None:
        recording_uid = None
        if self._recorder.state in (RecorderState.RECORDING, RecorderState.FINALIZING):
            current = self._recorder.current_meeting
            recording_uid = current.uid if current else None
        self._meeting_list.update_meetings(self._calendar.meetings, recording_uid)

    def _on_meeting_selected(self, meeting) -> None:  # pro budoucí použití
        log.debug("Vybrána schůzka: %s", getattr(meeting, "title", meeting))

    # --------------------------------------------------------------- nastavení

    def _open_settings(self) -> None:
        """Otevře dialog pro změnu tajné ICS adresy a po uložení obnoví kalendář."""
        from app.config import save_config

        url = IcsSetupDialog.get_url(self, initial=self._cfg.ics_url or "")
        if not url or url == self._cfg.ics_url:
            return  # zrušeno nebo beze změny
        self._cfg.ics_url = url
        try:
            save_config(self._cfg, "config.json")
        except Exception:  # noqa: BLE001
            log.exception("Uložení konfigurace selhalo")
            QMessageBox.warning(
                self,
                "Nastavení kalendáře",
                "Adresu se nepodařilo uložit do config.json.",
            )
            return
        # CalendarService čte cfg.ics_url přímo, takže stačí obnovit kalendář.
        self.refresh_calendar()

    # ------------------------------------------------------------------ tray

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore_window()

    def _restore_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._quitting:
            event.accept()
            return
        event.ignore()
        self.hide()
        if not self._tray_message_shown:
            self._tray_message_shown = True
            self._tray.showMessage(
                "Meeting Notetaker",
                "Aplikace běží na pozadí.",
                QSystemTrayIcon.MessageIcon.Information,
                4000,
            )

    def _quit(self) -> None:
        if self._recorder.state == RecorderState.RECORDING:
            answer = QMessageBox.question(
                self,
                "Ukončit aplikaci",
                "Právě probíhá nahrávání. Opravdu chcete zastavit záznam "
                "a ukončit aplikaci?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            try:
                self._recorder.stop()
            except Exception:  # noqa: BLE001
                log.exception("Zastavení záznamu při ukončování selhalo")
        # H6: počkat na doběhnutí kalendářového QThreadu, ať Qt neboří
        # QApplication/okno, zatímco vlákno běží blokující requests.get
        # ("QThread: Destroyed while thread is still running" -> pád/zamrznutí).
        worker = self._calendar_worker
        if worker is not None:
            try:
                worker.requestInterruption()
                if not worker.wait(2000):
                    log.warning("Kalendářový worker nedoběhl do 2 s při ukončování.")
            except Exception:  # noqa: BLE001
                log.exception("Čekání na kalendářový worker při ukončování selhalo.")
        self._quitting = True
        self._tray.hide()
        QApplication.quit()
