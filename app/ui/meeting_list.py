"""Levý panel: seznam dnešních a nadcházejících schůzek jako vizuální karty."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from dateutil.tz import tzlocal

from PySide6.QtCore import QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
)

from app.models import Meeting, Platform

log = logging.getLogger(__name__)

_ROLE_MEETING = Qt.ItemDataRole.UserRole + 1
_ROLE_STATE = Qt.ItemDataRole.UserRole + 2  # "recording" | "armed" | "next" | ""

# Název a barva odznaku podle platformy (barvy fungují na světlém i tmavém pozadí).
_PLATFORM = {
    Platform.MEET: ("Meet", "#2DA44E"),
    Platform.TEAMS: ("Teams", "#5B5FC7"),
}
_REC = "#E5484D"     # nahrává se
_ARMED = "#6366F1"   # připraveno k auto-záznamu


def _day_label(d: date, today: date) -> str:
    if d == today:
        return "Dnes"
    if d == today + timedelta(days=1):
        return "Zítra"
    return d.strftime("%d. %m. %Y")


class _MeetingDelegate(QStyledItemDelegate):
    """Vykreslí schůzku jako kartu, hlavičku dne jako jemný nadpis."""

    def sizeHint(self, option, index):  # noqa: N802
        if index.data(_ROLE_MEETING) is None:
            return QSize(0, 32)  # hlavička dne
        return QSize(0, 58)

    def paint(self, painter: QPainter, option, index):  # noqa: N802, C901
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pal = option.palette
        text_color = pal.color(QPalette.ColorRole.Text)
        meeting = index.data(_ROLE_MEETING)

        # --- hlavička dne -------------------------------------------------
        if meeting is None:
            muted = QColor(text_color)
            muted.setAlpha(135)
            f = QFont(option.font)
            f.setBold(True)
            f.setPointSizeF(max(option.font.pointSizeF() - 1.0, 7.0))
            painter.setFont(f)
            painter.setPen(QPen(muted))
            r = option.rect.adjusted(14, 6, -10, -4)
            painter.drawText(
                r,
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft,
                str(index.data(Qt.ItemDataRole.DisplayRole)).upper(),
            )
            painter.restore()
            return

        state = index.data(_ROLE_STATE) or ""
        r = option.rect.adjusted(6, 2, -8, -4)

        # --- pozadí karty (výběr / hover) ---------------------------------
        if option.state & QStyle.StateFlag.State_Selected:
            bg = QColor(pal.color(QPalette.ColorRole.Highlight))
            bg.setAlpha(48)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(QRectF(r), 9, 9)
        elif option.state & QStyle.StateFlag.State_MouseOver:
            bg = QColor(text_color)
            bg.setAlpha(16)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(bg)
            painter.drawRoundedRect(QRectF(r), 9, 9)

        # --- barva akcentu ------------------------------------------------
        plat_name, plat_color = _PLATFORM.get(meeting.platform, ("—", "#8A8F98"))
        if state == "recording":
            accent = QColor(_REC)
        elif state == "armed":
            accent = QColor(_ARMED)
        else:
            accent = QColor(plat_color)

        bar = QRectF(r.left() + 4, r.top() + 9, 3.5, r.height() - 18)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawRoundedRect(bar, 1.75, 1.75)

        left = int(bar.right()) + 12
        right = r.right() - 6

        # --- odznak platformy (vpravo nahoře) -----------------------------
        pill_w = 0
        if plat_name != "—":
            pf = QFont(option.font)
            pf.setPointSizeF(max(option.font.pointSizeF() - 1.5, 6.5))
            pfm = QFontMetrics(pf)
            pill_h = pfm.height() + 5
            pill_w = pfm.horizontalAdvance(plat_name) + 16
            pill = QRectF(right - pill_w, r.top() + 9, pill_w, pill_h)
            fill = QColor(plat_color)
            fill.setAlpha(38)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(pill, pill_h / 2, pill_h / 2)
            painter.setFont(pf)
            painter.setPen(QColor(plat_color))
            painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, plat_name)
            pill_w += 10  # odsazení od času

        # --- čas (horní řádek) --------------------------------------------
        tf = QFont(option.font)
        tf.setBold(True)
        tfm = QFontMetrics(tf)
        painter.setFont(tf)
        if state == "recording":
            painter.setPen(QColor(_REC))
        elif state == "armed":
            painter.setPen(QColor(_ARMED))
        else:
            painter.setPen(text_color)
        time_str = "{}–{}".format(
            meeting.start.strftime("%H:%M"), meeting.end.strftime("%H:%M")
        )
        time_rect = QRect(left, r.top() + 9, max(right - pill_w - left, 10), tfm.height())
        painter.drawText(
            time_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            time_str,
        )

        # --- titulek (spodní řádek) ---------------------------------------
        title_font = QFont(option.font)
        if state in ("recording", "armed", "next"):
            title_font.setBold(True)
        ttfm = QFontMetrics(title_font)
        painter.setFont(title_font)
        painter.setPen(QColor(_REC) if state == "recording" else text_color)
        title = meeting.title or ""
        if state == "recording":
            title = "● " + title  # ● indikátor nahrávání
        title_rect = QRect(left, r.bottom() - ttfm.height() - 8, right - left, ttfm.height())
        elided = ttfm.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(
            title_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            elided,
        )
        painter.restore()


class MeetingListWidget(QListWidget):
    """Seznam schůzek (karty) seskupený podle dne."""

    meeting_selected = Signal(object)  # Meeting

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._meetings: list[Meeting] = []
        self._recording_uid: str | None = None
        self._armed_uid: str | None = None
        self.setMinimumWidth(280)
        self.setItemDelegate(_MeetingDelegate(self))
        self.setMouseTracking(True)  # hover efekt karet
        self.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSpacing(0)
        self.itemClicked.connect(self._on_item_clicked)

    # ------------------------------------------------------------------ API

    def update_meetings(self, meetings: list[Meeting], recording_uid: str | None) -> None:
        self._meetings = list(meetings)
        self._recording_uid = recording_uid
        self._rebuild()

    def set_armed_uid(self, uid: str | None) -> None:
        """Zvýrazní schůzku připravenou k automatickému záznamu."""
        if uid != self._armed_uid:
            self._armed_uid = uid
            self._rebuild()

    # ------------------------------------------------------------- internal

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        meeting = item.data(_ROLE_MEETING)
        if meeting is not None:
            self.meeting_selected.emit(meeting)

    def _rebuild(self) -> None:
        self.clear()
        now = datetime.now(tz=tzlocal())
        today = now.date()

        # první nadcházející schůzka
        next_uid: str | None = None
        for m in self._meetings:
            if m.start >= now:
                next_uid = m.uid
                break

        current_day: date | None = None
        for m in self._meetings:
            day = m.start.date()
            if day != current_day:
                current_day = day
                sep = QListWidgetItem(_day_label(day, today))
                sep.setFlags(Qt.ItemFlag.NoItemFlags)
                self.addItem(sep)

            if m.uid == self._recording_uid:
                state = "recording"
            elif m.uid == self._armed_uid:
                state = "armed"
            elif m.uid == next_uid:
                state = "next"
            else:
                state = ""

            item = QListWidgetItem()
            item.setData(_ROLE_MEETING, m)
            item.setData(_ROLE_STATE, state)
            item.setText(m.title)  # fallback pro přístupnost
            item.setToolTip(
                "{}–{}  {}".format(
                    m.start.strftime("%H:%M"), m.end.strftime("%H:%M"), m.title
                )
            )
            self.addItem(item)
