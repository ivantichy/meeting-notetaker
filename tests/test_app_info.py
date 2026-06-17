"""Testy locatoru app-info.json (discoverability pro skilly/tasky).

Locator musí na PEVNÉ cestě (%LOCALAPPDATA%\\MeetingNotetaker\\app-info.json)
zveřejnit ABSOLUTNÍ ``notes_dir``/``index`` aktuálního běhu — ať dev i instalace
ukazují skutečné umístění přepisů. A nesmí NIKDY shodit start (defenzivní zápis).
"""
import json
import os

from app.app_info import app_info_path, resolve_glossary_path, write_app_info
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

    # glossary.txt je publikován jako ABSOLUTNÍ cesta v adresáři appky (app_dir).
    assert os.path.isabs(data["glossary"])
    assert data["glossary"] == os.path.join(data["app_dir"], "glossary.txt")


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


# --------------------------------------------------------------------------- #
# resolve_glossary_path — glossary.txt v adresáři appky, robustně dev/instalace #
# --------------------------------------------------------------------------- #

def test_resolve_glossary_prefers_app_info_app_dir(tmp_path, monkeypatch):
    """Resolver vybere app_dir z app-info.json, když ten adresář existuje, a
    vrátí v něm glossary.txt jako absolutní cestu."""
    import app.app_info as app_info

    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    # Izoluj od reálného dev checkoutu na stroji.
    monkeypatch.setattr(app_info, "DEV_APP_DIR", str(tmp_path / "no-dev"))
    monkeypatch.chdir(tmp_path)

    real_app = tmp_path / "real-app"
    real_app.mkdir()
    base = local / "MeetingNotetaker"
    base.mkdir(parents=True)
    (base / "app-info.json").write_text(
        json.dumps({"app_dir": str(real_app)}, ensure_ascii=False),
        encoding="utf-8",
    )

    resolved = resolve_glossary_path()
    assert resolved == os.path.join(os.path.abspath(str(real_app)), "glossary.txt")


def test_resolve_glossary_falls_back_to_cwd_when_nothing_exists(
    tmp_path, monkeypatch
):
    """Když žádný kandidátní adresář appky neexistuje, vrátí glossary.txt v cwd
    (absolutní) a NIKDY nevyhodí výjimku."""
    import app.app_info as app_info

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope"))
    monkeypatch.setattr(app_info, "DEV_APP_DIR", str(tmp_path / "no-dev"))
    monkeypatch.chdir(tmp_path)

    resolved = resolve_glossary_path()
    assert os.path.isabs(resolved)
    assert os.path.basename(resolved) == "glossary.txt"
    assert resolved == os.path.abspath("glossary.txt")


def test_resolve_glossary_never_raises_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    resolved = resolve_glossary_path()
    assert os.path.isabs(resolved)
    assert os.path.basename(resolved) == "glossary.txt"
