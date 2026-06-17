"""Slovník jmen a termínů pro ``initial_prompt`` Whisperu (kvalita přepisu).

Whisper bere ``initial_prompt`` jako kontext (max ~224 tokenů; když ho překročí,
faster-whisper si nechá POSLEDNÍ tokeny). Když do něj dáme správné tvary názvů,
jmen a IT/AI termínů, model je přepisuje správně místo foneticky zkomolených
verzí (např. elem6 -> "LM6", Claude -> "Klooda").

DVĚ ÚROVNĚ SLOVNÍKU:
  * vestavěné výchozí termíny ``GLOSSARY_TERMS`` (níž) — kompilují se do balíčku;
  * externí soubor ``glossary.txt`` v pracovním adresáři appky (tam, kde leží
    ``config.json``) — ZDROJ PRAVDY, uživatel ho edituje ZA BĚHU bez buildu
    (může termíny přidávat i MAZAT). Když chybí, vytvoří se předvyplněný
    vestavěnými termíny. Vestavěné ``GLOSSARY_TERMS`` se do promptu NEpřimíchávají
    natvrdo — jsou jen prvotní náplň souboru a fallback při chybě čtení.

Prompt sestavujeme PER MEETING z jeho dat (jména účastníků + název) — ne globálně
a ne zapečený do kešovaného modelu. Pořadí: úvodní věta + (název) na začátku,
slovník a jména meetingu na KONCI, ať přežijí ořez Whisperu. Účastníky cápneme
na ~12 jmen, ať dlouhý seznam nevytlačí slovník.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

#: Krátká uvozující věta promptu (udává jazyk/kontext). Schválně dvojjazyčná
#: stručná nápověda — funguje pro češtinu i angličtinu (multilingual=False
#: nechá Whisper detekovat jazyk jednou pro celé audio).
BASE_PROMPT = "Přepis pracovního meetingu o IT a vývoji softwaru."

#: Název externího editovatelného slovníku (relativní k pracovnímu adresáři
#: appky — main.py dělá chdir na kořen, takže to sedí v dev i v instalaci).
GLOSSARY_FILENAME = "glossary.txt"

#: Strop počtu jmen účastníků v promptu (token budget). Dlouhý seznam by jinak
#: vytlačil slovník přes ~224tokenový limit Whisperu.
MAX_ATTENDEES = 12

#: Hrubý strop délky promptu ve "slovech" (token budget, heuristika ~1 slovo ≈
#: 1 token). Drženo pod ~224, ať se prompt celý vejde a neořeže se.
MAX_PROMPT_WORDS = 200

#: Vestavěný výchozí slovník správných tvarů názvů/termínů, které se v přepisech
#: komolí. EDITOVAT ZA BĚHU lze v ``glossary.txt`` (vznikne předvyplněný tímto);
#: tady je jen prvotní náplň ``glossary.txt`` + fallback při chybě čtení.
GLOSSARY_TERMS: list[str] = [
    "elem6",
    "Unicorn",
    "Claude",
    "Codex",
    "hooks",
    "PowerShell",
    "React",
    "faster-whisper",
    "Whisper",
    "GitHub",
    "Python",
    "Cowork",
    "MCP",
    "Anthropic",
]

#: Komentářová hlavička, kterou předvyplníme nově vytvořený ``glossary.txt``.
_GLOSSARY_FILE_HEADER = (
    "# Slovník pro přepis (Whisper initial_prompt) — kvalita rozpoznání jmen a termínů.\n"
    "# Jeden termín na řádek. Prázdné řádky a řádky začínající '#' se ignorují.\n"
    "# Sem patří názvy nástrojů, značky, AI modely a slova, která se v přepisu komolí.\n"
    "# Soubor lze editovat za běhu — změny se projeví u dalšího přepisu (není nutný\n"
    "# nový build). Jména účastníků meetingu se doplňují automaticky z kalendáře.\n"
    "# Prompt držte krátký (~224 tokenů je strop Whisperu).\n"
)


def _glossary_path() -> str:
    """Cesta k editovatelnému slovníku (relativní k pracovnímu adresáři appky)."""
    return GLOSSARY_FILENAME


def ensure_glossary_file(path: "str | None" = None) -> str:
    """Zajistí existenci ``glossary.txt`` — když chybí, vytvoří ho předvyplněný
    vestavěnými termíny + českou komentářovou hlavičkou. Vrací použitou cestu.

    Chybu zápisu jen zaloguje (přepis ani UI nesmí kvůli ní spadnout) — slovník
    se pak prostě bere jen z vestavěných ``GLOSSARY_TERMS``.
    """
    p = path or _glossary_path()
    if os.path.exists(p):
        return p
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(_GLOSSARY_FILE_HEADER)
            f.write("\n")
            for term in GLOSSARY_TERMS:
                f.write(f"{term}\n")
    except OSError:
        log.exception("Vytvoření %s selhalo — používám vestavěný slovník.", p)
    return p


def _load_glossary_terms(path: "str | None" = None) -> list[str]:
    """Načte termíny z ``glossary.txt`` (vytvoří ho předvyplněný, když chybí).
    Soubor je ZDROJ PRAVDY — vestavěné ``GLOSSARY_TERMS`` slouží jen jako prvotní
    náplň nového souboru a jako fallback při chybě čtení; nepřimíchávají se natvrdo,
    takže uživatel může termíny i mazat. Prázdné řádky a ``#`` komentáře ignoruje,
    deduplikuje case-insensitivně (pořadí zachová). Chyba čtení -> vestavěné termíny.
    """
    p = path or _glossary_path()
    try:
        ensure_glossary_file(p)
        with open(p, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        log.exception("Čtení %s selhalo — používám vestavěný slovník.", p)
        return list(GLOSSARY_TERMS)

    file_terms: list[str] = []
    for line in raw_lines:
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        file_terms.append(term)

    # Soubor je ZDROJ PRAVDY (vestavěné termíny do něj jen prvotně nasázíme přes
    # ensure_glossary_file), takže uživatel může termíny i MAZAT. Dedup case-insens.
    return _dedup_preserve_order(file_terms)


def _dedup_preserve_order(items: "list[str]") -> list[str]:
    """Deduplikace bez ohledu na velikost písmen, se zachováním pořadí."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        key = it.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _clean_attendee_names(attendees: "list[str] | None") -> list[str]:
    """Z e-mailů/jmen účastníků udělá seznam pro prompt: e-mail -> lokální část
    (před ``@``), prázdné zahodí. Pořadí zachová (dedup řeší až výsledný merge).
    """
    names: list[str] = []
    for raw in attendees or []:
        name = (raw or "").strip()
        if not name:
            continue
        if "@" in name:
            # E-mail -> lokální část (jméno bývá v ní, doména je šum).
            name = name.split("@", 1)[0].strip()
        if name:
            names.append(name)
    return names


def build_initial_prompt(
    attendees: "list[str] | None" = None,
    title: "str | None" = None,
    glossary_path: "str | None" = None,
) -> str:
    """Sestaví ``initial_prompt`` PER MEETING: úvod (+ název) na začátku, slovník
    a jména účastníků na KONci (přežijí ořez Whisperu, který si nechá poslední
    tokeny).

    ``attendees`` jsou jména/e-maily účastníků meetingu (z kalendáře u živého
    přepisu, z frontmatteru u finálního; e-mail -> lokální část). ``title`` je
    název schůzky (volitelně). Slovník se načte čerstvě z ``glossary.txt``
    (zdroj pravdy; vestavěné termíny jen jako prvotní náplň souboru + fallback).

    Token budget: jména cápneme na ``MAX_ATTENDEES`` a celý prompt na zhruba
    ``MAX_PROMPT_WORDS`` slov — slovník přitom NIKDY nevypadne kvůli dlouhému
    seznamu účastníků (slovník i zkrácená jména jdou do promptu jako celek a
    ořezává se až úplný konec).
    """
    glossary_terms = _load_glossary_terms(glossary_path)
    names = _clean_attendee_names(attendees)[:MAX_ATTENDEES]

    # Slovník + jména meetingu jdou na KONEC (Whisper si při ořezu nechá konec).
    # Dedup společně, ať se jméno shodné s termínem neopakuje.
    tail_terms = _dedup_preserve_order(glossary_terms + names)

    # Úvod: base hint + (volitelně) název schůzky — to nese kontext, smí padnout
    # při ořezu jako první (proto je na začátku).
    head = BASE_PROMPT
    clean_title = (title or "").strip().replace("\n", " ").replace("\r", " ")
    if clean_title:
        head = f"{head} Téma: {clean_title}."

    if not tail_terms:
        prompt = head
    else:
        glossary = ", ".join(tail_terms)
        prompt = f"{head} Termíny a jména: {glossary}."

    return _truncate_to_word_budget(prompt)


def _truncate_to_word_budget(prompt: str, max_words: int = MAX_PROMPT_WORDS) -> str:
    """Hrubý token budget: když má prompt víc než ``max_words`` slov, nechá si
    POSLEDNÍCH ``max_words`` slov (stejně jako Whisper ořezává konec tokenů, kde
    máme schválně slovník + jména). U běžné velikosti slovníku se neuplatní.
    """
    words = prompt.split()
    if len(words) <= max_words:
        return prompt
    return " ".join(words[-max_words:])
