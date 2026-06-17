"""Testy READ-ONLY MCP serveru (app/mcp_server.py).

Testujeme podkladové čisté funkce (berou ``notes_dir`` argumentem) — bez živé
stdio smyčky: list vrací nejnovější první se správnými poli; search najde termín
case-insensitive a vrátí snippet; get_transcript vrátí obsah a ODMÍTNE traversal
i cesty mimo notes dir; resolver vybere existující adresář.
"""
import json
import os

import pytest

from app.app_info import resolve_notes_dir
from app.mcp_server import (
    _resolve_note_path,
    add_glossary_terms_tool,
    get_glossary_tool,
    get_today,
    get_transcript,
    list_recent_meetings,
    remove_glossary_terms_tool,
    search_transcripts,
)


# --------------------------------------------------------------------------- #
# Pomocné: postav dočasný notes dir s pár poznámkami + index.jsonl.            #
# --------------------------------------------------------------------------- #

def _note_md(title: str, start: str, body: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        f"start: {start}\n"
        "platform: teams\n"
        "status: done\n"
        "---\n\n"
        f"# {title}\n\n"
        "## Přepis\n"
        f"{body}\n"
    )


@pytest.fixture
def notes_dir(tmp_path):
    """Notes dir se dvěma poznámkami a index.jsonl (starší + novější záznam)."""
    d = tmp_path / "notes"
    d.mkdir()

    (d / "2026-06-12_1730_porada.md").write_text(
        _note_md(
            "Týmová porada",
            "2026-06-12T17:30:00+02:00",
            "[00:00:01] Ivan: Začínáme poradu.\n"
            "[00:00:05] Petr: Rozpočet na příští kvartál schválíme.\n",
        ),
        encoding="utf-8",
    )
    (d / "2026-06-15_1600_rozhovor.md").write_text(
        _note_md(
            "Online rozhovor",
            "2026-06-15T16:00:00+02:00",
            "[00:00:10] Ivan: Dobrý den.\n"
            "[00:00:20] Ostatní: Povíme si o ROLI a procesech.\n",
        ),
        encoding="utf-8",
    )

    index = [
        {
            "uid": "uid-porada-1",
            "title": "Týmová porada",
            "platform": "teams",
            "event_start": "2026-06-12T17:30:00+02:00",
            "recorded_start": "2026-06-12T17:30:04+02:00",
            "duration_min": 42.0,
            "note": "2026-06-12_1730_porada.md",
            "quality": "final",
        },
        {
            "uid": "uid-rozhovor-2",
            "title": "Online rozhovor",
            "platform": "teams",
            "event_start": "2026-06-15T16:00:00+02:00",
            "recorded_start": "2026-06-15T16:00:04+02:00",
            "duration_min": 88.2,
            "note": "2026-06-15_1600_rozhovor.md",
            "quality": "final",
        },
    ]
    with open(d / "index.jsonl", "w", encoding="utf-8") as f:
        for rec in index:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return str(d)


# --------------------------------------------------------------------------- #
# list_recent_meetings                                                         #
# --------------------------------------------------------------------------- #

def test_list_recent_meetings_newest_first_with_fields(notes_dir):
    rows = list_recent_meetings(notes_dir, limit=20)
    assert len(rows) == 2
    # Nejnovější (2026-06-15) první.
    assert rows[0]["note"] == "2026-06-15_1600_rozhovor.md"
    assert rows[1]["note"] == "2026-06-12_1730_porada.md"
    # Správná pole na každém záznamu.
    expected = {
        "uid",
        "title",
        "platform",
        "event_start",
        "recorded_start",
        "duration_min",
        "note",
        "quality",
    }
    assert set(rows[0].keys()) == expected
    assert rows[0]["title"] == "Online rozhovor"
    assert rows[0]["duration_min"] == 88.2


def test_list_recent_meetings_respects_limit(notes_dir):
    rows = list_recent_meetings(notes_dir, limit=1)
    assert len(rows) == 1
    assert rows[0]["note"] == "2026-06-15_1600_rozhovor.md"


def test_list_recent_meetings_missing_index_returns_empty(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_recent_meetings(str(empty)) == []


# --------------------------------------------------------------------------- #
# search_transcripts                                                           #
# --------------------------------------------------------------------------- #

def test_search_finds_term_with_snippet(notes_dir):
    hits = search_transcripts(notes_dir, "rozpočet")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["note"] == "2026-06-12_1730_porada.md"
    assert hit["title"] == "Týmová porada"
    assert "rozpo" in hit["snippet"].lower()


def test_search_is_case_insensitive(notes_dir):
    # "ROLI" je v souboru velkými písmeny; hledáme malými.
    lower = search_transcripts(notes_dir, "roli")
    upper = search_transcripts(notes_dir, "ROLI")
    assert [h["note"] for h in lower] == ["2026-06-15_1600_rozhovor.md"]
    assert [h["note"] for h in upper] == [h["note"] for h in lower]


def test_search_empty_query_returns_nothing(notes_dir):
    assert search_transcripts(notes_dir, "") == []
    assert search_transcripts(notes_dir, "   ") == []


def test_search_respects_limit(notes_dir):
    # "Ivan" je v obou poznámkách; limit=1 vrátí jen jednu.
    hits = search_transcripts(notes_dir, "Ivan", limit=1)
    assert len(hits) == 1


# --------------------------------------------------------------------------- #
# get_transcript + sanitizace cesty                                            #
# --------------------------------------------------------------------------- #

def test_get_transcript_by_filename(notes_dir):
    content = get_transcript(notes_dir, "2026-06-12_1730_porada.md")
    assert content is not None
    assert "## Přepis" in content
    assert "Začínáme poradu" in content


def test_get_transcript_without_md_suffix(notes_dir):
    content = get_transcript(notes_dir, "2026-06-12_1730_porada")
    assert content is not None
    assert "Začínáme poradu" in content


def test_get_transcript_by_uid(notes_dir):
    content = get_transcript(notes_dir, "uid-rozhovor-2")
    assert content is not None
    assert "Online rozhovor" in content


def test_get_transcript_unknown_returns_none(notes_dir):
    assert get_transcript(notes_dir, "neexistuje.md") is None


def test_get_transcript_rejects_traversal(tmp_path, notes_dir):
    # Tajný soubor o úroveň výš, mimo notes dir.
    secret = tmp_path / "secret.md"
    secret.write_text("TAJNÉ", encoding="utf-8")

    for evil in (
        "..\\secret.md",
        "../secret.md",
        "..//secret.md",
        os.path.join("..", "secret.md"),
        "subdir/../../secret.md",
    ):
        assert _resolve_note_path(notes_dir, evil) is None, evil
        assert get_transcript(notes_dir, evil) is None, evil


def test_get_transcript_rejects_absolute_path(tmp_path, notes_dir):
    secret = tmp_path / "secret.md"
    secret.write_text("TAJNÉ", encoding="utf-8")
    assert _resolve_note_path(notes_dir, str(secret)) is None
    assert get_transcript(notes_dir, str(secret)) is None
    # Absolutní cesta dovnitř notes diru se taky odmítne (musí být holé jméno).
    inside_abs = os.path.join(notes_dir, "2026-06-12_1730_porada.md")
    assert _resolve_note_path(notes_dir, inside_abs) is None


def test_resolve_note_path_accepts_inside_file(notes_dir):
    resolved = _resolve_note_path(notes_dir, "2026-06-12_1730_porada.md")
    assert resolved is not None
    assert os.path.isfile(resolved)
    assert os.path.realpath(notes_dir) == os.path.dirname(resolved)


# --------------------------------------------------------------------------- #
# get_today                                                                    #
# --------------------------------------------------------------------------- #

def test_get_today_filters_to_date(notes_dir):
    from datetime import date

    rows = get_today(notes_dir, today=date(2026, 6, 15))
    assert [r["note"] for r in rows] == ["2026-06-15_1600_rozhovor.md"]

    none_day = get_today(notes_dir, today=date(2020, 1, 1))
    assert none_day == []


# --------------------------------------------------------------------------- #
# resolve_notes_dir                                                            #
# --------------------------------------------------------------------------- #

def test_resolver_picks_existing_app_info_dir(tmp_path, monkeypatch):
    """Resolver vybere notes_dir z app-info.json, když ten adresář existuje.

    Dev/instalovaný kandidát ukážeme na neexistující cesty v tmp_path, aby test
    nezávisel na tom, jestli na stroji existuje skutečný dev checkout."""
    import app.app_info as app_info

    local = tmp_path / "LocalAppData"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    # Izoluj od reálného dev checkoutu na stroji.
    monkeypatch.setattr(
        app_info, "DEV_NOTES_DIR", str(tmp_path / "no-dev" / "notes")
    )
    monkeypatch.chdir(tmp_path)  # cwd/notes neexistuje

    real_notes = tmp_path / "real" / "notes"
    real_notes.mkdir(parents=True)

    base = local / "MeetingNotetaker"
    base.mkdir(parents=True)
    (base / "app-info.json").write_text(
        json.dumps({"notes_dir": str(real_notes)}, ensure_ascii=False),
        encoding="utf-8",
    )

    resolved = resolve_notes_dir()
    assert os.path.abspath(resolved) == os.path.abspath(str(real_notes))


def test_resolver_returns_absolute_default_when_nothing_exists(
    tmp_path, monkeypatch
):
    """Když žádný kandidát neexistuje, resolver vrátí absolutní rozumný default
    a NIKDY nevyhodí výjimku."""
    import app.app_info as app_info

    # LOCALAPPDATA na prázdný (neexistující) adresář -> app-info chybí,
    # instalovaný notes dir neexistuje; dev kandidát na neexistující cestu.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nope"))
    monkeypatch.setattr(
        app_info, "DEV_NOTES_DIR", str(tmp_path / "no-dev" / "notes")
    )
    monkeypatch.chdir(tmp_path)  # cwd/notes taky neexistuje (tmp_path je čistý)

    resolved = resolve_notes_dir()
    assert os.path.isabs(resolved)


def test_resolver_never_raises_without_localappdata(monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    resolved = resolve_notes_dir()
    assert os.path.isabs(resolved)


# --------------------------------------------------------------------------- #
# Glossary tools (the ONLY writable surface) — get/add/remove against a temp    #
# glossary.txt. We point resolve_glossary_path at tmp_path so tests don't touch #
# the real file. Transcripts stay read-only (no tool writes notes).            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def glossary_path(tmp_path, monkeypatch):
    """Namíří MCP glossary nástroje na glossary.txt v tmp_path."""
    import app.mcp_server as srv

    p = str(tmp_path / "glossary.txt")
    monkeypatch.setattr(srv, "resolve_glossary_path", lambda: p)
    return p


def test_get_glossary_tool_empty_then_creates_file(glossary_path):
    """get_glossary vrátí {"glossary": []} a vytvoří prázdný soubor na vyžádání."""
    assert not os.path.exists(glossary_path)
    out = json.loads(get_glossary_tool())
    assert out == {"glossary": []}
    assert os.path.exists(glossary_path)  # vytvořen (jen hlavička)


def test_add_glossary_terms_tool_adds_and_reports(glossary_path):
    """add_glossary_terms přidá termíny a vrátí {added, glossary}."""
    out = json.loads(add_glossary_terms_tool(["elem6", "Kubernetes"]))
    assert out["added"] == ["elem6", "Kubernetes"]
    assert out["glossary"] == ["elem6", "Kubernetes"]
    # potvrzení přes get_glossary (čte ze souboru)
    assert json.loads(get_glossary_tool())["glossary"] == ["elem6", "Kubernetes"]


def test_add_glossary_terms_tool_skips_existing_case_insensitive(glossary_path):
    """Už existující termín (case-insens.) se nepřidá podruhé — added je prázdné."""
    add_glossary_terms_tool(["elem6"])
    out = json.loads(add_glossary_terms_tool(["ELEM6", "Claude"]))
    assert out["added"] == ["Claude"]          # elem6 už byl, nepřidán
    assert out["glossary"] == ["elem6", "Claude"]


def test_remove_glossary_terms_tool_removes_and_reports(glossary_path):
    """remove_glossary_terms smaže termín (case-insens.) a vrátí {removed, glossary}."""
    add_glossary_terms_tool(["elem6", "Claude", "Kubernetes"])
    out = json.loads(remove_glossary_terms_tool(["CLAUDE"]))
    assert out["removed"] == ["Claude"]
    assert out["glossary"] == ["elem6", "Kubernetes"]
    # neznámý termín nic nesmaže
    out2 = json.loads(remove_glossary_terms_tool(["neexistuje"]))
    assert out2["removed"] == []
    assert out2["glossary"] == ["elem6", "Kubernetes"]


def test_glossary_tools_roundtrip_add_read_remove(glossary_path):
    """End-to-end: add -> get -> remove vrátí slovník zpět do prázdna."""
    add_glossary_terms_tool(["Foo", "Bar"])
    assert json.loads(get_glossary_tool())["glossary"] == ["Foo", "Bar"]
    out = json.loads(remove_glossary_terms_tool(["Foo", "Bar"]))
    assert out["removed"] == ["Foo", "Bar"]
    assert out["glossary"] == []
