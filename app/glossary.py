"""Slovník jmen a termínů pro ``initial_prompt`` Whisperu (kvalita přepisu).

Whisper bere ``initial_prompt`` jako kontext (max ~224 tokenů; když ho překročí,
faster-whisper si nechá POSLEDNÍ tokeny). Když do něj dáme správné tvary názvů,
jmen a termínů, model je přepisuje správně místo foneticky zkomolených
verzí (např. elem6 -> "LM6", Claude -> "Klooda").

ZDROJ SLOVNÍKU:
  * externí soubor ``glossary.txt`` v pracovním adresáři appky (tam, kde leží
    ``config.json``) — JEDINÝ ZDROJ termínů. Uživatel ho edituje ZA BĚHU bez
    buildu (termíny přidává i maže). Když chybí, vytvoří se PRÁZDNÝ (jen s českou
    komentářovou hlavičkou) — žádné vestavěné termíny se nepředvyplňují.
  * ``GLOSSARY_TERMS`` je schválně PRÁZDNÝ seznam: žádný vestavěný slovník není.
    Termíny pocházejí pouze z ``glossary.txt`` a z jmen účastníků dané schůzky.

Téma schůzky NENÍ zadrátované — ``BASE_PROMPT`` je neutrální a skutečné téma
nese až NÁZEV schůzky z kalendáře (``build_initial_prompt`` ho připojí jako
"Téma: <název>.").

Prompt sestavujeme PER MEETING z jeho dat (jména účastníků + název) — ne globálně
a ne zapečený do kešovaného modelu. Pořadí: úvodní věta + (název) na začátku,
slovník a jména meetingu na KONCI, ať přežijí ořez Whisperu. Účastníky cápneme
na ~12 jmen, ať dlouhý seznam nevytlačí slovník.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

#: Krátká uvozující věta promptu (udává kontext). Schválně TÉMATICKY NEUTRÁLNÍ —
#: skutečné téma nese až NÁZEV schůzky z kalendáře, který ``build_initial_prompt``
#: připojí jako "Téma: <název>." (nic o oboru/předmětu tu není zadrátováno).
BASE_PROMPT = "Přepis schůzky."

#: Název externího editovatelného slovníku (relativní k pracovnímu adresáři
#: appky — main.py dělá chdir na kořen, takže to sedí v dev i v instalaci).
GLOSSARY_FILENAME = "glossary.txt"

#: Strop počtu jmen účastníků v promptu (token budget). Dlouhý seznam by jinak
#: vytlačil slovník přes ~224tokenový limit Whisperu.
MAX_ATTENDEES = 12

#: Hrubý strop délky promptu ve "slovech" (token budget, heuristika ~1 slovo ≈
#: 1 token). Drženo pod ~224, ať se prompt celý vejde a neořeže se.
MAX_PROMPT_WORDS = 200

#: Strop počtu automaticky vytěžených tematických termínů (z názvu + popisu
#: schůzky). Drženo nízko: termíny jsou nejméně důvěryhodný, auto-extrahovaný
#: zdroj (precision over recall), jdou do HLAVY promptu a smí padnout při ořezu
#: jako první. Uživatelův slovník pokrývá opakující se termíny.
MAX_TOPIC_TERMS = 8

#: Stoplist pro extrakci tematických termínů: časté ALLCAPS/zkratky a tokeny,
#: které sice projdou tvarovým filtrem, ale jako bias do Whisperu nepatří (šum,
#: ne názvy). Porovnává se case-insensitivně.
_TOPIC_STOPWORDS = frozenset(
    {
        "OK", "WWW", "HTTP", "HTTPS", "PM", "AM", "ID", "RE", "FW", "FWD",
        "CET", "CEST", "UTC", "GMT", "TBD", "TBA", "FYI", "ASAP", "VS",
        "AKA", "ETA", "EOD", "POZVÁNKA", "ZOOM", "TEAMS", "MEET",
    }
)

#: Schválně PRÁZDNÝ — žádný vestavěný slovník neexistuje. Termíny pocházejí jen
#: z editovatelného ``glossary.txt`` (zdroj pravdy) a ze jmen účastníků schůzky.
#: Konstanta zůstává kvůli kompatibilitě: je to prvotní náplň nového souboru
#: (žádná) i fallback při chybě čtení (prázdný seznam = bez termínů; přijatelné).
GLOSSARY_TERMS: list[str] = []

#: Komentářová hlavička nově vytvořeného ``glossary.txt``. Soubor vzniká PRÁZDNÝ
#: (jen tato hlavička, žádné předvyplněné termíny) — naplní si ho uživatel.
_GLOSSARY_FILE_HEADER = (
    "# Slovník pro přepis (Whisper initial_prompt) — kvalita rozpoznání jmen a termínů.\n"
    "# Jeden termín na řádek. Prázdné řádky a řádky začínající '#' se ignorují.\n"
    "# Soubor začíná prázdný — žádné termíny nejsou předvyplněné, naplňte si ho sami.\n"
    "# Sem patří názvy nástrojů/produktů/značek, žargon a slova, která se v přepisu\n"
    "# komolí; případně jména kolegů, se kterými se často potkáváte.\n"
    "# Soubor lze editovat za běhu — změny se projeví u dalšího přepisu (není nutný\n"
    "# nový build). Jména účastníků schůzky se doplňují automaticky z kalendáře.\n"
    "# Prompt držte krátký (~224 tokenů je strop Whisperu).\n"
)


def _glossary_path() -> str:
    """Cesta k editovatelnému slovníku (relativní k pracovnímu adresáři appky)."""
    return GLOSSARY_FILENAME


def ensure_glossary_file(path: "str | None" = None) -> str:
    """Zajistí existenci ``glossary.txt`` — když chybí, vytvoří ho PRÁZDNÝ (jen
    s českou komentářovou hlavičkou; ``GLOSSARY_TERMS`` je prázdný, takže se
    žádné termíny nezapisují). Vrací použitou cestu.

    Chybu zápisu jen zaloguje (přepis ani UI nesmí kvůli ní spadnout) — slovník
    pak prostě zůstane prázdný (žádné termíny).
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
    """Načte termíny z ``glossary.txt`` (vytvoří ho prázdný, když chybí).
    Soubor je JEDINÝ ZDROJ termínů — vestavěný slovník neexistuje
    (``GLOSSARY_TERMS`` je prázdný), takže uživatel termíny libovolně přidává i
    maže. Prázdné řádky a ``#`` komentáře ignoruje, deduplikuje case-insensitivně
    (pořadí zachová). Chyba čtení -> prázdný seznam (bez termínů; přijatelné).
    """
    p = path or _glossary_path()
    try:
        ensure_glossary_file(p)
        with open(p, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError:
        log.exception("Čtení %s selhalo — slovník zůstane prázdný.", p)
        return list(GLOSSARY_TERMS)

    file_terms: list[str] = []
    for line in raw_lines:
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        file_terms.append(term)

    # Soubor je JEDINÝ ZDROJ termínů (žádný vestavěný slovník), takže uživatel
    # termíny libovolně přidává i MAZAT. Dedup case-insens.
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


#: Token vypadající jako datum/čas/verze/číslo (přeskočit i přes písmeno+číslici).
#: Pokrývá: 2026, 16:00, 10x/3d, v1.2.3, 1.2.3, datumy 12.6. apod.
_DATEISH_RE = __import__("re").compile(
    r"^(?:"
    r"v?\d+(?:[.:]\d+)+\.?"      # 1.2.3, 16:00, v1.2.3, 12.6.
    r"|\d+[.:]"                  # 12.  16:
    r"|\d+[a-zA-Z]"             # 10x, 3d, 2h  (číslo pak písmeno)
    r"|[a-zA-Z]?\d+"            # 2026, v1, x3 (volitelně 1 písmeno pak číslo)
    r")$"
)
#: Token je CamelCase/mixedCase: malé písmeno hned následované velkým
#: (PowerShell, GitHub, mixedCase). Jazykově neutrální (lat. písmena).
_CAMEL_RE = __import__("re").compile(r"[a-zěščřžýáíéúůóťďňľ][A-ZĚŠČŘŽÝÁÍÉÚŮÓŤĎŇĹ]")
#: ALLCAPS akronym délky 2-6 (MCP, CRM, API). Jen velká písmena (lat.).
_ACRONYM_RE = __import__("re").compile(r"^[A-Z]{2,6}$")


def _is_topic_term(token: str) -> bool:
    """Rozhodne, zda je ``token`` "identifikátorový" — přesně ten typ, který
    Whisper komolí — a má smysl ho přidat jako kontext do promptu.

    Token kvalifikuje, pokud splní ASPOŇ JEDNO (precision over recall):
      * CamelCase/mixedCase: malé písmeno hned následované velkým (PowerShell,
        GitHub, mixedCase);
      * obsahuje SOUČASNĚ písmeno i číslici a má délku >= 3 (elem6, GPT-4, S3),
        ale NENÍ čisté datum/čas/verze/číslo (2026, 16:00, 10x, v1.2.3);
      * ALLCAPS akronym délky 2-6 (MCP, CRM, API).

    Zamítá: čistě malá slova, "obyčejná" Slova s velkým prvním písmenem (česká
    věta začíná velkým -> šum), čísla, datumy/časy/verze.
    """
    if not token:
        return False
    # 1) CamelCase / mixedCase kdekoli v tokenu.
    if _CAMEL_RE.search(token):
        return True
    has_alpha = any(c.isalpha() for c in token)
    has_digit = any(c.isdigit() for c in token)
    # 2) písmeno + číslice, délka >= 3, ale ne datum/čas/verze/číslo.
    if has_alpha and has_digit and len(token) >= 3:
        if _DATEISH_RE.match(token):
            return False
        return True
    # 3) ALLCAPS akronym délky 2-6 (bez číslic; číslicové řeší pravidlo 2).
    if _ACRONYM_RE.match(token) and not has_digit:
        return True
    return False


def extract_topic_terms(title: str, description: str) -> list[str]:
    """Z ``title + description`` lokálně a deterministicky vytěží malou sadu
    "identifikátorových" termínů (názvy nástrojů/produktů, zkratky, kódy) pro
    kontext do initial_prompt. Žádné externí/LLM volání, čistá funkce.

    Postup: rozdělí text na tokeny (interpunkce kromě ``-``/``_``/``.`` uvnitř
    se ořeže z okrajů), nechá jen tokeny, které projdou ``_is_topic_term`` a
    nejsou ve stoplistu, deduplikuje case-insensitivně (zachová pořadí) a cápne
    na ``MAX_TOPIC_TERMS``. PRECISION OVER RECALL: radši termín vynechá, než aby
    do Whisperu vnesl falešný bias.
    """
    import re

    text = f"{title or ''} {description or ''}"
    # Tokenizace: rozsekat na bílých znacích a běžné interpunkci; pomlčku,
    # podtržítko a tečku necháme UVNITŘ (GPT-4, large-v3, elem6.io), ořežeme je
    # ale z okrajů (konce vět apod.).
    raw_tokens = re.split(r"[\s,;:!?()\[\]{}<>\"'„“”‚‘’/\\|@#]+", text)
    out: list[str] = []
    for tok in raw_tokens:
        tok = tok.strip("-_.")
        if not tok or len(tok) < 2:
            continue
        if not _is_topic_term(tok):
            continue
        if tok.casefold() in {s.casefold() for s in _TOPIC_STOPWORDS}:
            continue
        out.append(tok)
    return _dedup_preserve_order(out)[:MAX_TOPIC_TERMS]


def build_initial_prompt(
    attendees: "list[str] | None" = None,
    title: "str | None" = None,
    glossary_path: "str | None" = None,
    topic_terms: "list[str] | None" = None,
) -> str:
    """Sestaví ``initial_prompt`` PER MEETING: úvod (+ název + tematický kontext)
    na začátku, slovník a jména účastníků na KONci (přežijí ořez Whisperu, který
    si nechá poslední tokeny).

    ``attendees`` jsou jména/e-maily účastníků meetingu (z kalendáře u živého
    přepisu, z frontmatteru u finálního; e-mail -> lokální část). ``title`` je
    název schůzky z kalendáře (volitelně) — nese skutečné téma. Slovník se načte
    čerstvě z ``glossary.txt`` (jediný zdroj termínů, žádný vestavěný slovník).

    ``topic_terms`` jsou AUTOMATICKY vytěžené tematické termíny z popisu/názvu
    schůzky (viz ``extract_topic_terms``). Jako nejméně důvěryhodný (auto)
    zdroj jdou do HLAVY promptu (region "Kontext: …", hned za názvem), takže se
    při překročení rozpočtu OŘÍZNOU JAKO PRVNÍ — uživatelův slovník a reálná
    jména na konci mají vždy přednost. Deduplikují se proti slovníku i jménům.

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

    # Tematické termíny (auto) dedup proti slovníku+jménům (chráněný konec) —
    # ať se nezdvojí. Co už je na konci (důvěryhodné), do hlavy nedáváme.
    protected_keys = {t.casefold() for t in tail_terms}
    topic = [
        t for t in _dedup_preserve_order(topic_terms or [])
        if t and t.casefold() not in protected_keys
    ]

    # Úvod: base hint + (volitelně) název schůzky + (volitelně) tematický
    # kontext. To vše nese kontext a smí při ořezu padnout jako první (proto je
    # na začátku) — termíny jsou navíc auto-extrahované, tedy nejméně důvěryhodné.
    head = BASE_PROMPT
    clean_title = (title or "").strip().replace("\n", " ").replace("\r", " ")
    if clean_title:
        head = f"{head} Téma: {clean_title}."
    if topic:
        head = f"{head} Kontext: {', '.join(topic)}."

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
