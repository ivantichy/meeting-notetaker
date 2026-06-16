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
from app.scheduler import pick_action
from app.ui.call_panel import CallPanel
from app.ui.meeting_list import MeetingListWidget

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
        self._stopped_uids: set[str] = set()  # meetingy, které už nemáme znovu spouštět

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
        self._post_label.setStyleSheet("color: #fb8c00; padding-right: 6px;")
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
        recorder.on_state_changed.append(
            lambda s: self._bridge.state_changed.emit(getattr(s, "value", str(s)))
        )
        recorder.on_segment.append(
            lambda t0, t1, text: self._bridge.segment.emit(float(t0), float(t1), text)
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
        quit_action = QAction("Ukončit", self)
        quit_action.triggered.connect(self._quit)
        tray_menu.addAction(show_action)
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
            self.statusBar().setStyleSheet("color: #e53935;")
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

    def _scheduler_tick(self) -> None:
        now = datetime.now(tz=tzlocal())
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

        # Meeting, který už jednou nahrával a byl zastaven (ručně či
        # automaticky), znovu nespouštět — jinak plánovač restartuje
        # záznam každých 5 s až do konce události.
        if action in ("arm", "start") and meeting is not None and meeting.uid in self._stopped_uids:
            action = "none"

        if action == "arm":
            self._armed_uid = meeting.uid if meeting else None
            self._meeting_list.set_armed_uid(self._armed_uid)
            self._call_panel.set_next_meeting(meeting, armed=True)
        elif action == "start":
            # Nezačínat "naslepo" v čase události: počkat, až se hovor
            # skutečně rozběhne (Teams/prohlížeč drží mikrofon). Bez toho
            # vznikaly prázdné záznamy meetingů, na které se Ivan nepřipojil.
            if getattr(self._cfg, "detect_calls", True):
                from app.call_detector import active_call

                if active_call() is None:
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
            if getattr(self._cfg, "detect_calls", True):
                from app.call_detector import active_call

                if active_call() is not None:
                    return
            self._call_panel.request_stop()
        else:
            if self._armed_uid is not None and self._recorder.state == RecorderState.IDLE:
                self._armed_uid = None
                self._meeting_list.set_armed_uid(None)
            self._update_next_meeting_info()
            self._detector_tick(now)

    def _detector_tick(self, now: datetime) -> None:
        """Auto-detekce hovoru mimo kalendář: aplikace (Teams/prohlížeč)
        právě používá mikrofon -> spustit záznam; po uvolnění mikrofonu
        (+ grace) záznam zastavit. Záznamy řízené kalendářem nezastavuje."""
        if not getattr(self._cfg, "detect_calls", True):
            return
        import time as _time

        from app.call_detector import active_call

        label = active_call()
        state = self._recorder.state

        if state == RecorderState.RECORDING:
            current = self._recorder.current_meeting
            if current is None:
                return
            if current.uid != self._detector_uid:
                # Záznam řízený kalendářem: pokud byl hovor během záznamu
                # aspoň jednou aktivní a pak skončil (mikrofon uvolněn
                # > early_stop_grace_s), zastavíme dřív než v end+grace.
                from app.models import Platform

                if current.platform not in (Platform.MEET, Platform.TEAMS):
                    return  # ruční záznamy nezastavujeme
                if label:
                    self._call_seen_in_recording = True
                    self._call_last_seen = _time.monotonic()
                elif (
                    self._call_seen_in_recording
                    and _time.monotonic() - self._call_last_seen
                    > getattr(self._cfg, "early_stop_grace_s", 60)
                ):
                    log.info(
                        "Hovor skončil dřív než kalendářová událost — zastavuji záznam."
                    )
                    self._call_seen_in_recording = False
                    self._call_panel.request_stop()
                elif (
                    not self._call_seen_in_recording
                    and self._recorder.elapsed_s
                    > getattr(self._cfg, "no_call_timeout_s", 180)
                ):
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

    def _update_post_status(self) -> None:
        """Indikátor dopřepisování WAV: text ve stavové liště + oranžová
        tray ikona (pokud zrovna nenahráváme — červená má přednost)."""
        pp = self._post_processor
        if pp is None:
            return
        busy = pp.busy
        if busy:
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
        if busy:
            self._tray.setIcon(self._icon_postprocessing)
            self.setWindowIcon(self._icon_postprocessing)
            self._tray.setToolTip("Meeting Notetaker — dopřepisuji záznam")
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
            self._stopped_uids.add(self._last_recording_uid)
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

    def _update_meeting_list(self) -> None:
        recording_uid = None
        if self._recorder.state in (RecorderState.RECORDING, RecorderState.FINALIZING):
            current = self._recorder.current_meeting
            recording_uid = current.uid if current else None
        self._meeting_list.update_meetings(self._calendar.meetings, recording_uid)

    def _on_meeting_selected(self, meeting) -> None:  # pro budoucí použití
        log.debug("Vybrána schůzka: %s", getattr(meeting, "title", meeting))

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
        self._quitting = True
        self._tray.hide()
        QApplication.quit()
