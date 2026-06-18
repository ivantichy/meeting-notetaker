---
name: meeting-notetaker
description: Ivan's local bot-free meeting notetaker app on Windows — Granola-style, Czech transcription, no bot visible in calls. Reads/searches transcripts via the connected meeting-notetaker MCP server. Use whenever Ivan asks about his meetings, transcripts or call notes, or about operating the app. Czech — co jsme se domluvili na meetingu, shrnutí hovoru, přepis schůzky, zápisky z meetingu, co říkal X na callu, úkoly z porady, najdi v přepisech, funguje nahrávání, proč se nenahrál meeting, zkontroluj notetaker, spusť/vypni nahrávání, slovník přepisu. English — meeting transcript, call summary, action items from the call, did the notetaker record, find in transcripts, glossary. Trigger proactively for any question about what was said or decided in Ivan's calls, even without the word "notetaker".
---

# meeting-notetaker

Ivanova vlastní desktopová aplikace (Python + PySide6, postavená Claudem 06/2026)
pro nahrávání a český přepis meetingů **bez bota** — lokálně zachytává WASAPI
loopback + mikrofon jako Granola. Přístup k datům řeší **MCP server**; tahle
skill je tenká vrstva: routing na MCP + doménové znalosti, které popis MCP
nástroje neunese.

## Primární instrukce: použij MCP `meeting-notetaker`

Pro **jakýkoli dotaz na přepisy / meetingy / hovory** použij nástroje
připojeného MCP serveru **`meeting-notetaker`** — **nehledej soubory ručně**,
když je MCP dostupné:

- `list_recent_meetings` — poslední záznamy (přehled, „co bylo dneska/tento týden").
- `search_transcripts` — fulltext napříč přepisy („najdi v přepisech", „co říkal X").
- `get_transcript` — celý přepis konkrétního záznamu (pro shrnutí + úkoly).
- `get_today` — dnešní meetingy/přepisy („shrň mi dnešní call").
- Slovník (editovatelný): `get_glossary` (vypiš termíny), `add_glossary_terms`
  (přidej jména/názvy firem/produktů, ať je Whisper přepisuje správně),
  `remove_glossary_terms` (odeber). Glosář drž **malý a cílený** — jde do
  Whisperova `initial_prompt` s ~224 token stropem, takže nafouklý seznam se
  ořeže a může vytlačit jména účastníků daného meetingu; přidávej jen termíny,
  které se reálně komolí (jména, názvy firem/produktů, žargon), ne běžnou
  slovní zásobu — řádově desítky položek, ne stovky.

Přepisy a metadata jsou pro tyhle nástroje **read-only**; měnit jde jen slovník.

## Co MCP popis neunese (proč skill existuje)

### Granola běží vedle (souběh dvou notetakerů)
Ivan má na stejných meetinzích současně i Granolu (MCP tools
`query_granola_meetings`, `get_meeting_transcript` apod.).
- **Granola neumí pořádně česky** — u českých meetingů je její transkript vadný
  (zkomolený / přeložený nesmysl). Tahle aplikace česky umí, proto u **českých**
  meetingů ber přepisy odsud a Granolu **ignoruj**.
- U **anglických** meetingů jsou oba zdroje OK — Granola může mít navíc AI
  shrnutí. Při dotazu na konkrétní meeting můžeš oba zdroje porovnat.

### Živý vs finální přepis (kvalita)
Přepis vzniká dvoufázově: živě model `small`, po skončení se přepíše znovu
kvalitním `large-v3-turbo` (s rozlišením mluvčích Ivan/Ostatní). V metadatech to
poznáš podle `transcript_quality: final` (živý záznam toto pole nemá). **Pokud
vedle poznámky pořád leží `.wav`, finální dopřepsání ještě čeká/běží** — živý
přepis je tedy provizorní a horší. „Ostatní" = kdokoli z protistrany (nerozlišuje
jednotlivé osoby; konkrétní jména dovozuj z účastníků a kontextu).

### Soukromí
Vše běží lokálně, audio neopouští počítač. **Na rozdíl od meeting botů účastníci
nahrávání nevidí ani nedostanou upozornění** — u externích hovorů Ivanovi
připomeň slušnost/povinnost je informovat.

### Provoz a debugging aplikace
Lokální PySide6 appka; běží buď **nainstalovaná** (per-user, autostart při
přihlášení, ikona v tray), nebo z **dev checkoutu** (`C:\temp\Claude\meeting-notetaker`,
`pythonw -m app.main`). Auto-záznam jen u událostí s Meet/Teams linkem; start
kopíruje skutečné připojení k hovoru, ne čas v kalendáři.

- **Běží appka?** Hledej proces `MeetingNotetaker.exe` (instalace) nebo
  `pythonw.exe`/`python.exe` s `app.main` (dev). Restart = ukončit proces a
  spustit znovu (instalace: `MeetingNotetaker.exe`; dev: `pythonw -m app.main`).
  Změny v `config.json` se projeví až po restartu.
- **„Proč se meeting nenahrál?"** → 1) běží appka? 2) měla událost Meet/Teams
  link? (bez linku se auto-záznam nespustí) 3) logy: technický `notetaker.log`
  a deník hovorů `notes\hovory.log` (`PŘEPIS START/HOTOVO/CHYBA`, výpadky audia
  jako `data discontinuity`).

## Fallback bez MCP (když server `meeting-notetaker` není připojený)
Teprve když MCP **není** dostupné, najdi přepisy v souborech (čti `Read`/`Grep`):
1. `%LOCALAPPDATA%\MeetingNotetaker\app-info.json` → vezmi `notes_dir` a `index`
   (zdroj pravdy pro běžící build);
2. jinak nainstalovaný build `%LOCALAPPDATA%\Programs\MeetingNotetaker\notes`;
3. jinak dev build `C:\temp\Claude\meeting-notetaker\notes`.

Poznámky jsou markdown s YAML frontmatterem (`title`, `start`/`end`, `platform`,
`attendees`, `status`, `transcript_quality`) + sekce `## Přepis` s řádky
`[HH:MM:SS] text` (živě) nebo `[HH:MM:SS] Ivan: / Ostatní:` (finál).
