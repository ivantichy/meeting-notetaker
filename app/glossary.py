"""Slovník jmen a termínů pro ``initial_prompt`` Whisperu (kvalita přepisu).

Whisper bere ``initial_prompt`` jako kontext (max ~224 tokenů). Když do něj
dáme správné tvary názvů, jmen a IT/AI termínů, model je přepisuje správně
místo foneticky zkomolených verzí (např. elem6 -> "LM6", Claude -> "Klooda").

JAK SLOVNÍK UPRAVIT: stačí editovat ``GLOSSARY_TERMS`` níž — přidávejte/měňte
položky podle toho, co se v přepisech komolí. Účastníky meetingu doplňuje
``build_initial_prompt`` automaticky (z kalendáře/frontmatteru), sem patří jen
obecné termíny a značky. Prompt držte krátký (~224 tokenů je strop Whisperu).
"""
from __future__ import annotations

#: Krátká uvozující věta promptu (udává jazyk/kontext). Schválně dvojjazyčná
#: stručná nápověda — funguje pro češtinu i angličtinu (multilingual=False
#: nechá Whisper detekovat jazyk jednou pro celé audio).
BASE_PROMPT = "Přepis pracovního meetingu o IT a vývoji softwaru."

#: Slovník správných tvarů názvů/termínů, které se v přepisech komolí.
#: EDITUJTE TADY: přidejte cokoli, co model přepisuje špatně (názvy nástrojů,
#: značky, AI modely, knihovny). Známé zkomoleniny: elem6, Unicorn, Claude,
#: Codex, hooks, PowerShell, React, faster-whisper, Whisper, GitHub, Python,
#: Cowork, MCP, Anthropic.
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


def build_initial_prompt(attendees: "list[str] | None" = None) -> str:
    """Sestaví ``initial_prompt`` = base hint + slovník termínů + jména účastníků.

    ``attendees`` jsou jména/e-maily účastníků meetingu (z kalendáře u živého
    přepisu, z frontmatteru u finálního). Z e-mailů vezme jen lokální část
    (před ``@``). Duplicity a prázdné hodnoty zahodí, pořadí zachová. Prompt
    je tím delší, čím víc je termínů — držíme ho proto stručný (slovník +
    pár jmen se do ~224 tokenů Whisperu pohodlně vejde).
    """
    terms: list[str] = list(GLOSSARY_TERMS)
    for raw in attendees or []:
        name = (raw or "").strip()
        if not name:
            continue
        # E-mail -> lokální část (jméno bývá v ní, doména je šum).
        if "@" in name:
            name = name.split("@", 1)[0].strip()
        if name:
            terms.append(name)

    # Deduplikace bez ohledu na velikost písmen, zachování pořadí.
    seen: set[str] = set()
    unique: list[str] = []
    for t in terms:
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(t)

    glossary = ", ".join(unique)
    if not glossary:
        return BASE_PROMPT
    return f"{BASE_PROMPT} Termíny a jména: {glossary}."
