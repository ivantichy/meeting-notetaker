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


def test_file_autocreated_empty_with_header_when_missing(tmp_path):
    """Když glossary.txt chybí, vytvoří se PRÁZDNÝ — jen s českou komentářovou
    hlavičkou, žádné předvyplněné termíny (vestavěný slovník neexistuje)."""
    p = _gp(tmp_path)
    assert not os.path.exists(p)
    ensure_glossary_file(p)
    assert os.path.exists(p)
    content = (tmp_path / "glossary.txt").read_text(encoding="utf-8")
    # komentářová hlavička (česky)
    assert content.lstrip().startswith("#")
    assert "Jeden termín na řádek" in content
    # GLOSSARY_TERMS je prázdný, takže soubor neobsahuje žádnou ne-komentářovou řádku
    assert GLOSSARY_TERMS == []
    non_comment = [
        ln for ln in content.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]
    assert non_comment == []


def test_build_prompt_creates_file_on_demand(tmp_path):
    """build_initial_prompt si soubor vytvoří, když chybí. Nově vzniklý soubor je
    prázdný (žádné vestavěné termíny) -> prompt je jen base (bez sekce termínů)."""
    p = _gp(tmp_path)
    prompt = build_initial_prompt([], glossary_path=p)
    assert os.path.exists(p)
    assert prompt == BASE_PROMPT  # prázdný slovník + bez jmen/názvu -> jen base
    assert "Termíny a jména:" not in prompt


def test_file_is_source_of_truth_terms_removable(tmp_path):
    """Soubor je JEDINÝ ZDROJ termínů: v promptu je přesně to, co je v souboru,
    nic víc (žádný vestavěný slovník). Case-insens. dedup; chybějící termín chybí."""
    p = _gp(tmp_path)
    # 'claude'/'Claude' duplicita; 'Kubernetes' nový; 'elem6' v souboru NENÍ.
    (tmp_path / "glossary.txt").write_text(
        "claude\nClaude\nKubernetes\n", encoding="utf-8"
    )
    prompt = build_initial_prompt([], glossary_path=p)
    assert "Kubernetes" in prompt               # termín ze souboru
    assert prompt.lower().count("claude") == 1  # dedup case-insensitive
    assert "elem6" not in prompt                # není v souboru -> není v promptu


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


def test_read_error_falls_back_to_no_terms(tmp_path, monkeypatch):
    """Chyba čtení souboru -> tichý fallback na PRÁZDNÝ slovník (žádné termíny;
    vestavěný slovník neexistuje). Jména účastníků se přesto doplní normálně."""
    import app.glossary as g

    p = _gp(tmp_path)

    # ensure_glossary_file projde, ale open() pak vyhodí OSError.
    def _boom(*a, **kw):
        raise OSError("disk plný")

    monkeypatch.setattr(g, "ensure_glossary_file", lambda path=None: path or p)
    monkeypatch.setattr("builtins.open", _boom)
    prompt = build_initial_prompt(["Petr Novák"], glossary_path=p)
    # fallback je prázdný slovník -> žádné termíny, ale jméno účastníka tam je
    assert "Petr Novák" in prompt
    assert "Claude" not in prompt
    assert "elem6" not in prompt


def test_long_attendee_list_capped_glossary_still_present(tmp_path):
    """Dlouhý seznam účastníků se cápne na MAX_ATTENDEES a slovník zůstává."""
    p = _gp(tmp_path)
    # Uživatelský termín v souboru — musí v promptu přežít cápnutí jmen.
    (tmp_path / "glossary.txt").write_text("Kubernetes\nelem6\n", encoding="utf-8")
    many = [f"Ucastnik{i}" for i in range(40)]
    prompt = build_initial_prompt(many, glossary_path=p)
    # slovník nevypadl
    assert "Kubernetes" in prompt
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
    (tmp_path / "glossary.txt").write_text("Kubernetes\n", encoding="utf-8")
    prompt = build_initial_prompt(
        ["Petr Novák"], title="Plánování sprintu", glossary_path=p
    )
    assert prompt.startswith(BASE_PROMPT)
    # název je v úvodní (přední) části, slovník+jméno až za "Termíny a jména:"
    i_title = prompt.index("Plánování sprintu")
    i_terms = prompt.index("Termíny a jména:")
    assert i_title < i_terms
    assert prompt.index("Kubernetes") > i_terms
    assert prompt.index("Petr Novák") > i_terms


def test_huge_attendee_list_does_not_push_out_glossary(tmp_path):
    """I při extrémně dlouhém seznamu (přes word budget) zůstává slovník
    v promptu — ořez bere konec, kde je slovník + (cápnutá) jména."""
    p = _gp(tmp_path)
    # Uživatelské termíny v souboru — musí přežít i ořez word budgetu.
    (tmp_path / "glossary.txt").write_text("Kubernetes\nelem6\n", encoding="utf-8")
    many = [f"VelmiDlouheJmenoUcastnika{i}" for i in range(200)]
    prompt = build_initial_prompt(many, glossary_path=p)
    assert "Kubernetes" in prompt
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


def test_no_attendees_no_title_empty_glossary_is_base_only(tmp_path):
    """Bez jmen, bez názvu a s čerstvě vytvořeným (prázdným) slovníkem je prompt
    jen base hint — žádná sekce 'Termíny a jména:' (není co vypsat)."""
    p = _gp(tmp_path)
    prompt = build_initial_prompt(None, glossary_path=p)
    assert prompt == BASE_PROMPT
    assert "Termíny a jména:" not in prompt
    assert "Téma:" not in prompt  # bez názvu
