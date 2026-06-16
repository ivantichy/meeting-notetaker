"""Uživatelsky přívětivý dialog pro zadání tajné ICS adresy Google Kalendáře.

Zobrazí vysvětlení, číslované kroky, jak adresu získat, a pole pro vložení URL
s průběžnou validací (povolena jen https adresa vypadající jako ICS feed).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)


def _looks_like_ics(url: str) -> bool:
    """Vrátí True, jen když text vypadá jako https ICS feed.

    Pravidla: musí to být https:// adresa a buď obsahovat "/ical/"
    (typické pro Google tajnou iCal adresu), nebo končit na ".ics".
    """
    text = url.strip()
    if not text.lower().startswith("https://"):
        return False
    low = text.lower()
    # případné query parametry odřízneme jen pro test koncovky .ics
    path = low.split("?", 1)[0].split("#", 1)[0]
    return "/ical/" in low or path.endswith(".ics")


class IcsSetupDialog(QDialog):
    """Dialog pro zadání / změnu tajné ICS adresy Google Kalendáře."""

    def __init__(self, parent: QWidget | None = None, initial: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Nastavení kalendáře")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        intro = QLabel(
            "Meeting Notetaker potřebuje vědět, kdy máte schůzky, aby je mohl "
            "automaticky nahrávat. Čte je z vašeho Google Kalendáře přes tzv. "
            "<b>tajnou adresu ve formátu iCal</b> — to je soukromý odkaz, "
            "přes který aplikace jen čte události vašeho kalendáře. Odkaz "
            "nikam neposílá a zůstává uložen pouze na tomto počítači."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        steps_title = QLabel("Jak adresu získáte:")
        steps_title.setStyleSheet("font-weight: 600; margin-top: 4px;")
        layout.addWidget(steps_title)

        steps = QLabel(
            "1. Otevřete <b>Google Kalendář</b> v prohlížeči.<br>"
            "2. Vpravo nahoře klikněte na ozubené kolo → <b>Nastavení</b>.<br>"
            "3. Vlevo v sekci <b>„Nastavení mého kalendáře“</b> vyberte svůj "
            "kalendář.<br>"
            "4. Sjeďte dolů na <b>„Integrovat kalendář“</b>.<br>"
            "5. Zkopírujte hodnotu <b>„Tajná adresa ve formátu iCal“</b><br>"
            "&nbsp;&nbsp;&nbsp;(končí na <i>.ics</i>) a vložte ji níže."
        )
        steps.setWordWrap(True)
        steps.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(steps)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        field_label = QLabel("Tajná adresa ve formátu iCal:")
        layout.addWidget(field_label)

        self._edit = QLineEdit(self)
        self._edit.setText(initial or "")
        self._edit.setPlaceholderText(
            "https://calendar.google.com/calendar/ical/.../basic.ics"
        )
        self._edit.setClearButtonEnabled(True)
        self._edit.textChanged.connect(self._validate)
        layout.addWidget(self._edit)

        self._hint = QLabel("")
        self._hint.setWordWrap(True)
        self._hint.setStyleSheet("color: #e53935;")
        layout.addWidget(self._hint)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("Uložit")
        self._buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Zrušit")
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._validate(self._edit.text())

    def _validate(self, text: str) -> None:
        """Průběžná validace: tlačítko Uložit je aktivní jen pro platnou adresu."""
        cleaned = text.strip()
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if not cleaned:
            self._hint.setText("")
            ok_btn.setEnabled(False)
            return
        if _looks_like_ics(cleaned):
            self._hint.setStyleSheet("color: #2e7d32;")
            self._hint.setText("Adresa vypadá v pořádku.")
            ok_btn.setEnabled(True)
        else:
            self._hint.setStyleSheet("color: #e53935;")
            self._hint.setText(
                "Adresa musí začínat na https:// a být odkazem na iCal feed "
                "(obsahovat „/ical/“ nebo končit na „.ics“)."
            )
            ok_btn.setEnabled(False)

    def cleaned_url(self) -> str:
        """Vrátí očištěnou (oříznutou) zadanou adresu."""
        return self._edit.text().strip()

    @classmethod
    def get_url(
        cls, parent: QWidget | None = None, initial: str = ""
    ) -> "str | None":
        """Zobrazí dialog a vrátí očištěnou adresu, nebo None při zrušení.

        Pokud uživatel potvrdí prázdné/neplatné pole (nemělo by nastat, OK je
        v tom případě neaktivní), vrátí None.
        """
        dialog = cls(parent, initial=initial)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            url = dialog.cleaned_url()
            return url or None
        return None
