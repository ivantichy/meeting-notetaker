"""Testy editovatelného slovníku (glossary.txt) a sestavení initial_prompt.

Externí soubor se v testech vždy adresuje přes ``glossary_path`` na ``tmp_path``,
ať testy nezávisí na pracovním adresáři ani nevytvářejí soubor v repu.
"""
from __future__ import annotations

import os

from app.glossary import (
    BASE_PROMPT,
    GLOSSARY_TERMS,
    MAX_ATTENDEES,
    build_initial_prompt,
    ensure_glossary_file,
)


def _gp(tmp_path) -> str:
    return str(tmp_path / "glossary.txt")


def test_file_autocreated_with_defaults_when_missing(tmp_path):
    """Když glossary.txt chybí, vytvoří se předvyplněný vestavěnými termíny
    + českou komentářovou hlavičkou."""
    p = _gp(tmp_path)
    assert not os.path.exists(p)
    ensure_glossary_file(p)
    assert os.path.exists(p)
    content = (tmp_path / "glossary.txt").read_text(encoding="utf-8")
    # komentářová hlavička (česky) + všechny vestavěné termíny
    assert content.lstrip().startswith("#")
    assert "Jeden termín na řádek" in content
    for term in GLOSSARY_TERMS:
        assert term in content


def test_build_prompt_creates_file_on_demand(tmp_path):
    """build_initial_prompt si soubor vytvoří, když chybí (a použije defaulty)."""
    p = _gp(tmp_path)
    prompt = build_initial_prompt([], glossary_path=p)
    assert os.path.exists(p)
    assert "Claude" in prompt  # vestavěný termín v promptu


def test_file_is_source_of_truth_terms_removable(tmp_path):
    """Soubor je ZDROJ PRAVDY: vestavěné se nepřimíchávají natvrdo, takže termín
    v souboru vynechaný se v promptu neobjeví (jdou mazat). Case-insens. dedup."""
    p = _gp(tmp_path)
    # Soubor BEZ vestavěného 'elem6'; 'claude'/'Claude' duplicita; 'Kubernetes' nový.
    (tmp_path / "glossary.txt").write_text(
        "claude\nClaude\nKubernetes\n", encoding="utf-8"
    )
    prompt = build_initial_prompt([], glossary_path=p)
    assert "Kubernetes" in prompt               # termín ze souboru
    assert prompt.lower().count("claude") == 1  # dedup case-insensitive
    assert "elem6" not in prompt                # vestavěný NENÍ natvrdo přimíchán


def test_blank_lines_and_comments_ignored(tmp_path):
    """Prázdné řádky a '#' komentáře se ze souboru ignorují."""
    p = _gp(tmp_path)
    (tmp_path / "glossary.txt").write_text(
        "# komentář na začátku\n\n   \nNovyTermin\n# další komentář\n",
        encoding="utf-8",
    )
    prompt = build_initial_prompt([], glossary_path=p)
    assert "NovyTermin" in prompt
    assert "komentář" not in prompt  # komentář se nedostal do promptu


def test_read_error_falls_back_to_builtins(tmp_path, monkeypatch):
    """Chyba čtení souboru -> tichý fallback na vestavěné termíny."""
    import app.glossary as g

    p = _gp(tmp_path)

    # ensure_glossary_file projde, ale open() pak vyhodí OSError.
    def _boom(*a, **kw):
        raise OSError("disk plný")

    monkeypatch.setattr(g, "ensure_glossary_file", lambda path=None: path or p)
    monkeypatch.setattr("builtins.open", _boom)
    prompt = build_initial_prompt(["Petr Novák"], glossary_path=p)
    # vestavěné termíny tam jsou; čtení selhalo, takže žádné soubor-specifické
    assert "Claude" in prompt
    assert "elem6" in prompt


def test_long_attendee_list_capped_glossary_still_present(tmp_path):
    """Dlouhý seznam účastníků se cápne na MAX_ATTENDEES a slovník zůstává."""
    p = _gp(tmp_path)
    many = [f"Ucastnik{i}" for i in range(40)]
    prompt = build_initial_prompt(many, glossary_path=p)
    # slovník nevypadl
    assert "Claude" in prompt
    assert "elem6" in prompt
    # počet jmen v promptu je omezený (jen prvních MAX_ATTENDEES se vyskytuje)
    present = [n for n in many if n in prompt]
    assert len(present) == MAX_ATTENDEES
    assert "Ucastnik0" in prompt
    assert "Ucastnik39" not in prompt  # 40. jméno už se nevešlo


def test_prompt_ordering_glossary_and_names_at_end(tmp_path):
    """Pořadí: úvod (+ název) na začátku, slovník a jména na KONci (přežijí
    ořez Whisperu, který si nechá poslední tokeny)."""
    p = _gp(tmp_path)
    prompt = build_initial_prompt(
        ["Petr Novák"], title="Plánování sprintu", glossary_path=p
    )
    assert prompt.startswith(BASE_PROMPT)
    # název je v úvodní (přední) části, slovník+jméno až za "Termíny a jména:"
    i_title = prompt.index("Plánování sprintu")
    i_terms = prompt.index("Termíny a jména:")
    assert i_title < i_terms
    assert prompt.index("Claude") > i_terms
    assert prompt.index("Petr Novák") > i_terms


def test_huge_attendee_list_does_not_push_out_glossary(tmp_path):
    """I při extrémně dlouhém seznamu (přes word budget) zůstává slovník
    v promptu — ořez bere konec, kde je slovník + (cápnutá) jména."""
    p = _gp(tmp_path)
    many = [f"VelmiDlouheJmenoUcastnika{i}" for i in range(200)]
    prompt = build_initial_prompt(many, glossary_path=p)
    assert "Claude" in prompt
    assert "elem6" in prompt
    # prompt nepřeroste hrubý word budget o moc (heuristika ~200 slov)
    assert len(prompt.split()) <= 210


def test_email_attendees_use_local_part(tmp_path):
    """E-mail účastníka -> v promptu jen lokální část (před @)."""
    p = _gp(tmp_path)
    prompt = build_initial_prompt(
        ["ivan@example.com", "Petr Novák"], glossary_path=p
    )
    assert "ivan" in prompt
    assert "example.com" not in prompt
    assert "Petr Novák" in prompt


def test_no_attendees_no_title_is_base_plus_glossary(tmp_path):
    """Bez jmen a názvu je prompt base hint + slovník."""
    p = _gp(tmp_path)
    prompt = build_initial_prompt(None, glossary_path=p)
    assert prompt.startswith(BASE_PROMPT)
    assert "Termíny a jména:" in prompt
    assert "Téma:" not in prompt  # bez názvu
