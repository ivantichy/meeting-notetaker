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
    MAX_TOPIC_TERMS,
    build_initial_prompt,
    ensure_glossary_file,
    extract_topic_terms,
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


# ----------------------------------------------- extract_topic_terms (B)


class TestExtractTopicTerms:
    """Lokální deterministická extrakce 'identifikátorových' termínů z názvu +
    popisu schůzky. Precision over recall: radši termín vynechá, než aby vnesl
    falešný bias do Whisperu."""

    def test_recognizes_camelcase(self):
        terms = extract_topic_terms("Migrace na PowerShell", "Nasadíme GitHub Actions")
        assert "PowerShell" in terms
        assert "GitHub" in terms

    def test_recognizes_letter_digit_tokens(self):
        terms = extract_topic_terms("Integrace elem6", "Vyzkoušíme GPT-4 model")
        assert "elem6" in terms
        assert "GPT-4" in terms

    def test_recognizes_allcaps_acronyms(self):
        terms = extract_topic_terms("MCP server", "Napojení na CRM a API")
        assert "MCP" in terms
        assert "CRM" in terms
        assert "API" in terms

    def test_rejects_plain_words(self):
        """Obyčejná malá i Kapitalizovaná slova (česká věta) se neberou."""
        terms = extract_topic_terms(
            "Plánování sprintu", "Probereme rozpočet a termíny dodávky"
        )
        # žádné běžné slovo (malé ani s velkým prvním písmenem) neprošlo
        assert terms == []

    def test_rejects_dates_times_versions_numbers(self):
        """2026, 16:00, v1.2.3, 10x i čistá čísla se zahodí (i přes písmeno+číslici)."""
        terms = extract_topic_terms(
            "Schůzka 2026",
            "Začátek v 16:00, verze v1.2.3, zrychlení 10x, kapacita 12345",
        )
        for bad in ("2026", "16:00", "v1.2.3", "10x", "12345"):
            assert bad not in terms
        assert terms == []  # nic z toho nekvalifikuje

    def test_dedup_case_insensitive(self):
        terms = extract_topic_terms("PowerShell powershell PowerShell", "GitHub github")
        # case-insens. dedup -> každý termín jednou
        assert len(terms) == len([t for t in terms])
        lowered = [t.casefold() for t in terms]
        assert lowered.count("powershell") == 1
        assert lowered.count("github") == 1

    def test_caps_at_max_topic_terms(self):
        many = " ".join(f"Foo{i}Bar" for i in range(MAX_TOPIC_TERMS + 10))
        terms = extract_topic_terms(many, "")
        assert len(terms) == MAX_TOPIC_TERMS

    def test_empty_inputs_yield_empty(self):
        assert extract_topic_terms("", "") == []
        assert extract_topic_terms(None, None) == []  # type: ignore[arg-type]


# --------------------------------------- build_initial_prompt + topic_terms


def test_topic_terms_appear_in_prompt_head_context(tmp_path):
    """topic_terms se objeví v promptu jako 'Kontext: …' v HLAVĚ (za názvem,
    PŘED chráněným koncem se slovníkem a jmény)."""
    p = _gp(tmp_path)
    (tmp_path / "glossary.txt").write_text("Kubernetes\n", encoding="utf-8")
    prompt = build_initial_prompt(
        ["Petr Novák"],
        title="Plánování",
        glossary_path=p,
        topic_terms=["PowerShell", "elem6"],
    )
    assert "Kontext:" in prompt
    assert "PowerShell" in prompt
    assert "elem6" in prompt
    # pořadí: Téma -> Kontext (termíny) -> Termíny a jména (slovník + jména)
    i_title = prompt.index("Plánování")
    i_context = prompt.index("Kontext:")
    i_tail = prompt.index("Termíny a jména:")
    assert i_title < i_context < i_tail
    # termíny jsou v hlavě (před chráněným koncem)
    assert prompt.index("PowerShell") < i_tail
    # slovník i jméno jsou na chráněném konci
    assert prompt.index("Kubernetes") > i_tail
    assert prompt.index("Petr Novák") > i_tail


def test_topic_terms_deduped_against_glossary_and_names(tmp_path):
    """topic_terms se nezdvojí proti slovníku ani jménům (ta jsou důvěryhodnější,
    zůstávají na konci; z hlavy se duplicita vyhodí)."""
    p = _gp(tmp_path)
    (tmp_path / "glossary.txt").write_text("Kubernetes\n", encoding="utf-8")
    prompt = build_initial_prompt(
        ["PowerShell"],  # jméno shodné s termínem
        glossary_path=p,
        topic_terms=["Kubernetes", "PowerShell", "elem6"],
    )
    # Kubernetes (slovník) a PowerShell (jméno) zůstávají jen na konci, ne v Kontextu
    head = prompt.split("Termíny a jména:")[0]
    assert "Kubernetes" not in head
    assert "PowerShell" not in head
    # elem6 (jen v topic_terms) je v hlavě jako kontext
    assert "elem6" in head
    # každý se vyskytuje právě jednou
    assert prompt.count("Kubernetes") == 1
    assert prompt.count("PowerShell") == 1


def test_over_budget_trims_topic_terms_before_glossary_and_names(tmp_path):
    """Při překročení rozpočtu se OŘÍZNOU tematické termíny (hlava) DŘÍV než
    slovník a jména (chráněný konec, který si Whisper nechá)."""
    p = _gp(tmp_path)
    (tmp_path / "glossary.txt").write_text("DulezityTermin\n", encoding="utf-8")
    # Spousta tematických termínů (každý projde filtrem: CamelCase) -> přetečou
    # rozpočet a musí padnout jako první. MAX_TOPIC_TERMS jich omezí na 8, tak
    # je doplníme dlouhým názvem, ať hlava nafoukne prompt přes word budget.
    long_title = " ".join(f"VelmiDlouhyKontextovyTermin{i}" for i in range(300))
    prompt = build_initial_prompt(
        ["DuleziteJmeno"],
        title=long_title,
        glossary_path=p,
        topic_terms=["KontextovyTermin1", "KontextovyTermin2"],
    )
    # chráněný konec (slovník + jméno) přežil ořez
    assert "DulezityTermin" in prompt
    assert "DuleziteJmeno" in prompt
    # hlava (název/kontext) byla ořezána -> termíny z hlavy zmizely
    assert "VelmiDlouhyKontextovyTermin0" not in prompt
    # prompt nepřeroste hrubý word budget o moc
    assert len(prompt.split()) <= 210


def test_no_topic_terms_behaves_as_before(tmp_path):
    """Bez topic_terms (ruční záznam) je prompt jako dřív — žádná sekce Kontext."""
    p = _gp(tmp_path)
    (tmp_path / "glossary.txt").write_text("Kubernetes\n", encoding="utf-8")
    prompt = build_initial_prompt(["Petr"], title="Porada", glossary_path=p)
    assert "Kontext:" not in prompt
    assert "Kubernetes" in prompt
    assert "Petr" in prompt
