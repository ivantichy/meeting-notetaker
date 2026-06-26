"""Pravý panel: stav aktuálního hovoru + živý přepis."""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from dateutil.tz import tzlocal

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.models import Meeting, RecorderState
from app.ui.theme import STATUS_DOWNLOADING, STATUS_PROCESSING, STATUS_RECORDING

log = logging.getLogger(__name__)


def _fmt_hms(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_ms(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    m, s = divmod(seconds, 60)
    return f"{m:02d}:{s:02d}"


class CallPanel(QWidget):
    """Zobrazuje stav záznamu, živý přepis a ovládací tlačítko."""

    _stop_finished = Signal(str)  # "" = OK, jinak text chyby

    def __init__(self, cfg, recorder, model_warmup=None, parent=None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self._recorder = recorder
        #: Handle na předstažení modelů (W1) — pro gating ručního startu a indikaci.
        self._warmup = model_warmup
        self._state = RecorderState.IDLE
        self._next_meeting: Meeting | None = None
        self._armed = False
        self._recording_started: datetime | None = None
        self._note_path: str | None = None
        self._stopping = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        self._state_label = QLabel("Žádný hovor neběží")
        font = self._state_label.font()
        font.setPointSize(16)
        font.setBold(True)
        self._state_label.setFont(font)
        self._state_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._state_label)

        self._title_label = QLabel("")
        self._title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_label.setWordWrap(True)
        layout.addWidget(self._title_label)

        self._next_label = QLabel("")
        self._next_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._next_label)

        self._countdown_label = QLabel("")
        self._countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._countdown_label.setStyleSheet(
            f"color: {STATUS_RECORDING}; font-weight: bold;"
        )
        layout.addWidget(self._countdown_label)

        self._elapsed_label = QLabel("")
        ef = self._elapsed_label.font()
        ef.setPointSize(14)
        self._elapsed_label.setFont(ef)
        self._elapsed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._elapsed_label)

        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlaceholderText("Živý přepis se zobrazí zde…")
        # M6: strop bloků — vícehodinový hovor jinak nahromadí tisíce řádků a
        # autoscroll na obřím dokumentu se zpomaluje. Plný přepis je stejně v .md.
        self._transcript.setMaximumBlockCount(2000)
        layout.addWidget(self._transcript, stretch=1)

        self._queue_label = QLabel("")
        self._queue_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._queue_label)

        self._saved_label = QLabel("")
        self._saved_label.setWordWrap(True)
        self._saved_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._saved_label)

        self._button = QPushButton("● Nahrát teď")
        self._button.clicked.connect(self._on_button_clicked)
        layout.addWidget(self._button)

        self._stop_finished.connect(self._on_stop_finished, Qt.ConnectionType.QueuedConnection)

        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(1000)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start()

        self._apply_state()

    # ------------------------------------------------------------------ API

    def set_state(self, state: RecorderState) -> None:
        if state == RecorderState.RECORDING and self._state != RecorderState.RECORDING:
            self._recording_started = datetime.now(tz=tzlocal())
            self._transcript.clear()
            self._saved_label.setText("")
        if (
            state == RecorderState.IDLE
            and self._state in (RecorderState.RECORDING, RecorderState.FINALIZING)
        ):
            note_path = self._note_path or getattr(self._recorder, "note_path", None)
            if note_path:
                self._saved_label.setText(f"Uloženo: {note_path}")
        self._state = state
        if state in (RecorderState.RECORDING, RecorderState.FINALIZING):
            self._note_path = getattr(self._recorder, "note_path", None) or self._note_path
        self._apply_state()

    def set_next_meeting(self, meeting: Meeting | None, armed: bool) -> None:
        self._next_meeting = meeting
        self._armed = armed and meeting is not None
        self._update_idle_info()

    def append_segment(self, t0: float, t1: float, text: str) -> None:
        line = f"[{_fmt_hms(t0)}] {text}"
        sb = self._transcript.verticalScrollBar()
        # L3: autoscroll jen když je uživatel u dna — jinak respektuj, že si
        # odscrolloval nahoru (číst starší část), a nestrhávej ho zpět dolů.
        at_bottom = sb.value() >= sb.maximum() - 4
        self._transcript.appendPlainText(line)
        if at_bottom:
            sb.setValue(sb.maximum())

    def request_stop(self) -> None:
        """Zastaví záznam na pozadí (stop dotahuje frontu přepisu a může trvat)."""
        if self._stopping:
            return
        self._stopping = True
        self._button.setEnabled(False)
        self._button.setText("Dokončuji přepis…")
        self._state_label.setText("Dokončuji přepis…")
        self._state_label.setStyleSheet(f"color: {STATUS_PROCESSING};")

        def _worker() -> None:
            err = ""
            try:
                self._recorder.stop()
            except Exception as exc:  # noqa: BLE001
                log.exception("Chyba při zastavování záznamu")
                err = str(exc)
            self._stop_finished.emit(err)

        threading.Thread(target=_worker, name="recorder-stop", daemon=True).start()

    # ------------------------------------------------------------- internal

    def _on_stop_finished(self, err: str) -> None:
        self._stopping = False
        self._button.setEnabled(True)
        self._apply_state()
        if err:
            QMessageBox.critical(
                self, "Chyba záznamu", f"Zastavení záznamu se nezdařilo:\n{err}"
            )

    def trigger_record_now(self) -> None:
        """Spustí ruční záznam (pro tlačítko v horní liště)."""
        if self._state in (RecorderState.IDLE, RecorderState.ARMED):
            self._on_button_clicked()

    def _on_button_clicked(self) -> None:
        if self._state == RecorderState.RECORDING:
            self.request_stop()
        elif self._state in (RecorderState.IDLE, RecorderState.ARMED):
            # W1: dokud se model živého přepisu stahuje, ruční záznam nespouštíme
            # (jinak by spadl na „Unable to open model.bin") — dáme lidskou hlášku.
            if not self._live_model_ready():
                self._warn_model_not_ready()
                return
            try:
                self._recorder.start_manual()
            except Exception as exc:  # noqa: BLE001
                log.exception("Ruční start záznamu selhal")
                QMessageBox.critical(
                    self,
                    "Chyba záznamu",
                    "Záznam se nepodařilo spustit. Přepisovací model možná ještě "
                    f"není připravený — zkuste to za chvíli.\n\nDetail: {exc}",
                )

    def _live_model_ready(self) -> bool:
        """Je model živého přepisu připravený (reálný model.bin)? Když ne, ruční
        záznam nespouštíme (W1/W2) — ``model_store.is_ready`` kontroluje reálný
        soubor + kompletní sadu."""
        try:
            from app import model_store

            return model_store.is_ready(self._cfg.live_model)
        except Exception:  # noqa: BLE001
            return True

    def _downloading_model(self) -> "str | None":
        """Název modelu, který se právě předstahuje (z warm-up handle), nebo None."""
        wu = self._warmup
        if wu is not None:
            try:
                return wu.downloading
            except Exception:  # noqa: BLE001
                return None
        return None

    def _warn_model_not_ready(self) -> None:
        """Informativní (ne chybová) hláška: model se ještě stahuje, zkus později."""
        name = self._downloading_model()
        detail = (
            f"Model „{name}“ se právě stahuje. "
            if name
            else "Přepisovací model se ještě stahuje. "
        )
        QMessageBox.information(
            self,
            "Stahuji model",
            detail
            + "Záznam půjde spustit, jakmile bude model stažený — průběh vidíte "
            "ve stavové liště a podle modré ikony v oznamovací oblasti.",
        )

    def _apply_state(self) -> None:
        recording = self._state == RecorderState.RECORDING
        finalizing = self._state == RecorderState.FINALIZING or self._stopping

        if finalizing:
            self._state_label.setText("Dokončuji přepis…")
            self._state_label.setStyleSheet(f"color: {STATUS_PROCESSING};")
            self._button.setEnabled(False)
            self._button.setText("Dokončuji přepis…")
        elif recording:
            self._state_label.setText("● NAHRÁVÁ SE")
            self._state_label.setStyleSheet(f"color: {STATUS_RECORDING};")
            meeting = getattr(self._recorder, "current_meeting", None)
            self._title_label.setText(meeting.title if meeting else "")
            self._button.setEnabled(True)
            self._button.setText("■ Zastavit záznam")
        else:
            self._state_label.setText("Žádný hovor neběží")
            self._state_label.setStyleSheet("")
            self._title_label.setText("")
            self._elapsed_label.setText("")
            self._queue_label.setText("")
            self._button.setEnabled(True)
            self._button.setText("● Nahrát teď")

        self._title_label.setVisible(recording or finalizing)
        self._elapsed_label.setVisible(recording)
        self._transcript.setVisible(recording or finalizing)
        self._queue_label.setVisible(False)  # zobrazí se až po zjištění hloubky fronty
        self._next_label.setVisible(not recording and not finalizing)
        self._countdown_label.setVisible(not recording and not finalizing)
        self._update_idle_info()

    def _update_idle_info(self) -> None:
        if self._state in (RecorderState.RECORDING, RecorderState.FINALIZING):
            return
        # W1: když se přepisovací model stahuje, dej to výrazně najevo v panelu.
        dl = self._downloading_model()
        if dl is not None:
            self._next_label.setText(
                f"⏳ Stahuji přepisovací model „{dl}“… Záznam bude dostupný po stažení."
            )
            self._next_label.setStyleSheet(f"color: {STATUS_DOWNLOADING};")
            self._countdown_label.setText("")
            return
        self._next_label.setStyleSheet("")
        if self._next_meeting is None:
            self._next_label.setText("Žádná další schůzka v kalendáři.")
            self._countdown_label.setText("")
            return
        m = self._next_meeting
        self._next_label.setText(f"Další: {m.start.strftime('%H:%M')} {m.title}")
        if self._armed:
            remaining = (m.start - datetime.now(tz=tzlocal())).total_seconds()
            self._countdown_label.setText(f"Auto-záznam za {_fmt_ms(remaining)}")
        else:
            self._countdown_label.setText("")

    def _queue_depth(self) -> int | None:
        """Hloubka fronty přepisu, pokud ji recorder/transcriber nabízí."""
        try:
            depth = getattr(self._recorder, "queue_depth", None)
            if isinstance(depth, int):
                return depth
            for attr in ("transcriber", "_transcriber"):
                tr = getattr(self._recorder, attr, None)
                if tr is not None:
                    depth = getattr(tr, "queue_depth", None)
                    if isinstance(depth, int):
                        return depth
        except Exception:  # noqa: BLE001
            pass
        return None

    def _tick(self) -> None:
        if self._state == RecorderState.RECORDING:
            elapsed = getattr(self._recorder, "elapsed_s", None)
            if not isinstance(elapsed, (int, float)) or elapsed <= 0:
                if self._recording_started is not None:
                    elapsed = (
                        datetime.now(tz=tzlocal()) - self._recording_started
                    ).total_seconds()
                else:
                    elapsed = 0
            self._elapsed_label.setText(_fmt_hms(elapsed))
            depth = self._queue_depth()
            if depth is not None:
                self._queue_label.setText(f"Fronta přepisu: {depth}")
                self._queue_label.setVisible(True)
            else:
                self._queue_label.setVisible(False)
        else:
            self._update_idle_info()
