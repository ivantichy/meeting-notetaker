"""Jednotné vizuální téma aplikace Meeting Notetaker.

Pouze vzhled — nemění žádné chování. Aplikuje se v app.main hned po vytvoření
QApplication voláním apply_theme(app). Volí světlou/tmavou variantu podle
nastavení Windows a reaguje i na změnu režimu za běhu. Akcent je indigový;
červená zůstává vyhrazená pro stav „nahrává se" (řídí ji přímo widgety).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


class _Scheme:
    """Sada barev jednoho režimu (světlý/tmavý)."""

    def __init__(self, **kw) -> None:
        self.__dict__.update(kw)


LIGHT = _Scheme(
    window="#f4f5f7",
    surface="#ffffff",
    surface_alt="#eceef2",
    border="#d9dce2",
    text="#1f2329",
    text_muted="#6b7280",
    accent="#4f46e5",
    accent_hover="#4338ca",
    accent_pressed="#3730a3",
    accent_text="#ffffff",
    selection="#e0e3fb",
    scrollbar="#c3c8d1",
)

DARK = _Scheme(
    window="#1e1f22",
    surface="#26282d",
    surface_alt="#2e3137",
    border="#3a3d44",
    text="#e6e8eb",
    text_muted="#9aa0a6",
    accent="#818cf8",
    accent_hover="#99a3ff",
    accent_pressed="#6b78e6",
    accent_text="#11131a",
    selection="#39406b",
    scrollbar="#474c55",
)


_QSS = """
QListWidget {
    background-color: __SURFACE__;
    border: 1px solid __BORDER__;
    border-radius: 10px;
    padding: 6px;
    outline: 0;
}

QPlainTextEdit {
    background-color: __SURFACE__;
    border: 1px solid __BORDER__;
    border-radius: 10px;
    padding: 10px;
    selection-background-color: __ACCENT__;
    selection-color: __ACCENT_TEXT__;
}

QPushButton {
    background-color: __ACCENT__;
    color: __ACCENT_TEXT__;
    border: none;
    border-radius: 8px;
    padding: 9px 18px;
    font-weight: 600;
}
QPushButton:hover { background-color: __ACCENT_HOVER__; }
QPushButton:pressed { background-color: __ACCENT_PRESSED__; }
QPushButton:disabled { background-color: __SURFACE_ALT__; color: __TEXT_MUTED__; }

QToolBar {
    background-color: __WINDOW__;
    border: none;
    border-bottom: 1px solid __BORDER__;
    padding: 6px 8px;
    spacing: 6px;
}
QToolButton {
    background-color: transparent;
    color: __TEXT__;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 6px 12px;
    font-weight: 600;
}
QToolButton:hover { background-color: __SURFACE_ALT__; border-color: __BORDER__; }
QToolButton:pressed { background-color: __SELECTION__; }
QToolBar::separator { background: __BORDER__; width: 1px; margin: 5px 6px; }

QStatusBar { background-color: __WINDOW__; color: __TEXT_MUTED__; border-top: 1px solid __BORDER__; }
QStatusBar::item { border: none; }

QMenu { background-color: __SURFACE__; border: 1px solid __BORDER__; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 20px; border-radius: 6px; }
QMenu::item:selected { background-color: __SELECTION__; }
QMenu::separator { height: 1px; background: __BORDER__; margin: 4px 8px; }

QToolTip { background-color: __SURFACE__; color: __TEXT__; border: 1px solid __BORDER__; padding: 4px 8px; }

QSplitter::handle { background-color: __BORDER__; }
QSplitter::handle:horizontal { width: 1px; }

QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
QScrollBar::handle:vertical { background: __SCROLLBAR__; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: __TEXT_MUTED__; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
QScrollBar::handle:horizontal { background: __SCROLLBAR__; border-radius: 5px; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: __TEXT_MUTED__; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
"""


def _qss(c: _Scheme) -> str:
    """Doplní barvy do šablony. Delší tokeny nahrazuj dřív (jsou nadmnožinou kratších)."""
    out = _QSS
    for token, value in (
        ("__WINDOW__", c.window),
        ("__SURFACE_ALT__", c.surface_alt),
        ("__SURFACE__", c.surface),
        ("__BORDER__", c.border),
        ("__TEXT_MUTED__", c.text_muted),
        ("__ACCENT_HOVER__", c.accent_hover),
        ("__ACCENT_PRESSED__", c.accent_pressed),
        ("__ACCENT_TEXT__", c.accent_text),
        ("__ACCENT__", c.accent),
        ("__SELECTION__", c.selection),
        ("__SCROLLBAR__", c.scrollbar),
        ("__TEXT__", c.text),
    ):
        out = out.replace(token, value)
    return out


def _palette(c: _Scheme) -> QPalette:
    p = QPalette()
    role = QPalette.ColorRole
    p.setColor(role.Window, QColor(c.window))
    p.setColor(role.WindowText, QColor(c.text))
    p.setColor(role.Base, QColor(c.surface))
    p.setColor(role.AlternateBase, QColor(c.surface_alt))
    p.setColor(role.Text, QColor(c.text))
    p.setColor(role.Button, QColor(c.surface))
    p.setColor(role.ButtonText, QColor(c.text))
    p.setColor(role.BrightText, QColor("#ffffff"))
    p.setColor(role.ToolTipBase, QColor(c.surface))
    p.setColor(role.ToolTipText, QColor(c.text))
    p.setColor(role.PlaceholderText, QColor(c.text_muted))
    p.setColor(role.Highlight, QColor(c.accent))
    p.setColor(role.HighlightedText, QColor(c.accent_text))
    p.setColor(role.Link, QColor(c.accent))
    for r in (role.WindowText, role.Text, role.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, r, QColor(c.text_muted))
    return p


def _is_dark(app: QApplication) -> bool:
    """Tmavý režim Windows? Primárně z colorScheme(), jinak odhad z palety."""
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception:  # noqa: BLE001 — starší Qt bez colorScheme()
        pass
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def _apply(app: QApplication) -> None:
    c = DARK if _is_dark(app) else LIGHT
    app.setPalette(_palette(c))
    app.setStyleSheet(_qss(c))


def apply_theme(app: QApplication) -> None:
    """Nastaví Fusion styl, písmo, paletu a QSS; sleduje i změnu režimu Windows."""
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    _apply(app)
    try:
        app.styleHints().colorSchemeChanged.connect(lambda *_: _apply(app))
    except Exception:  # noqa: BLE001 — signál nemusí být v starším Qt
        pass
