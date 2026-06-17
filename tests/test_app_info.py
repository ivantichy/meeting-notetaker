"""Testy locatoru app-info.json (discoverability pro skilly/tasky).

Locator musí na PEVNÉ cestě (%LOCALAPPDATA%\\MeetingNotetaker\\app-info.json)
zveřejnit ABSOLUTNÍ ``notes_dir``/``index`` aktuálního běhu — ať dev i instalace
ukazují skutečné umístění přepisů. A nesmí NIKDY shodit start (defenzivní zápis).
"""
import json
import os

from app.app_info import app_info_path, write_app_info
from app.config import AppConfig


def test_write_produces_valid_json_with_absolute_paths(tmp_path, monkeypatch):
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    # notes_dir relativní (jako v configu) — locator ho musí zabsolutnit vůči cwd.
    cfg = AppConfig(notes_dir="notes")
    write_app_info(cfg)

    path = app_info_path()
    assert path is not None
    assert os.path.exists(path)
    # Locator leží v pevném base diru %LOCALAPPDATA%\MeetingNotetaker.
    assert os.path.dirname(path) == str(local / "MeetingNotetaker")

    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    assert data["transcripts"] is True
    assert data["app"] == "Meeting Notetaker"
    assert data["transcript_format"]  # neprázdný popis formátu
    assert data["updated"]            # ISO timestamp

    # notes_dir i index musí být ABSOLUTNÍ a vzájemně konzistentní.
    assert os.path.isabs(data["notes_dir"])
    assert os.path.isabs(data["index"])
    assert os.path.isabs(data["app_dir"])
    assert data["notes_dir"] == os.path.abspath("notes")
    assert data["index"] == os.path.join(data["notes_dir"], "index.jsonl")


def test_absolute_notes_dir_preserved(tmp_path, monkeypatch):
    """Absolutní notes_dir (nainstalovaný build) zůstane absolutní beze změny."""
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    installed_notes = str(tmp_path / "Programs" / "MeetingNotetaker" / "notes")

    cfg = AppConfig(notes_dir=installed_notes)
    write_app_info(cfg)

    with open(app_info_path(), encoding="utf-8") as f:
        data = json.load(f)
    assert data["notes_dir"] == os.path.abspath(installed_notes)


def test_overwrites_existing(tmp_path, monkeypatch):
    """Druhý běh přepíše locator (žádný leftover .tmp soubor)."""
    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    write_app_info(AppConfig(notes_dir="notes-a"))
    write_app_info(AppConfig(notes_dir="notes-b"))

    with open(app_info_path(), encoding="utf-8") as f:
        data = json.load(f)
    assert data["notes_dir"] == os.path.abspath("notes-b")

    base = local / "MeetingNotetaker"
    leftovers = [n for n in os.listdir(base) if ".tmp" in n]
    assert leftovers == []


def test_missing_localappdata_does_not_raise(tmp_path, monkeypatch):
    """Chybějící LOCALAPPDATA -> tiše přeskočit, žádná výjimka, žádný soubor."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    # Nesmí vyhodit výjimku.
    write_app_info(AppConfig(notes_dir="notes"))
    assert app_info_path() is None


def test_unwritable_base_dir_does_not_raise(tmp_path, monkeypatch):
    """Když base dir nelze vytvořit (kolize se SOUBOREM), zápis se jen zaloguje."""
    # Nasměrujeme LOCALAPPDATA na existující SOUBOR — os.makedirs pak selže.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(blocker))

    # Nesmí vyhodit výjimku (defenzivní try/except).
    write_app_info(AppConfig(notes_dir="notes"))
