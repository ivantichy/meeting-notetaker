# CLAUDE.md — pracovní pravidla pro tento repozitář

Meeting Notetaker = lokální Windows appka (PySide6) pro nahrávání a český přepis
meetingů bez bota; přepis přes faster-whisper / CTranslate2. Dvě distribuce:
**dev** (`python -m app.main`) a **nainstalovaný PyInstaller build** (autostart,
tray). Detaily architektury jsou v `ARCHITECTURE.md`, uživatelský popis v `README.md`.

Tenhle soubor je závazný checklist a konvence — drž se ho při KAŽDÉ změně.

## Checklist pro každou shippable změnu (nic z toho nevynechávej)

1. **Vždy aktualizuj dokumentaci.** Když se mění chování/architektura, uprav
   `ARCHITECTURE.md` a (když se to týká uživatele) `README.md` ve stejné dávce.
2. **Vždy spusť KOMPLETNÍ testy + UI import-smoke a měj je zelené** PŘED vydáním:
   `./.venv/Scripts/python.exe -m pytest -q` a
   `QT_QPA_PLATFORM=offscreen python -c "import app.ui.main_window, app.main"`.
   Nový kód = nové testy.
3. **Vždy vydej přes GitHub Actions, nikdy nebuilduj installer lokálně.**
   `git push origin main`, pak `git tag -a vX.Y.Z -m "<smysluplná zpráva>"` a
   `git push origin vX.Y.Z`. Workflow `release.yml` zbuilduje installer a publikuje.
4. **Vždy zvyš verzi.** Verze installeru se bere z gitového tagu (workflow předává
   `/DMyAppVersion` do ISCC) — `MyAppVersion` v `.iss` NIKDY nehardcoduj. Vyber
   další semver (vX.Y.Z).
5. **Vždy popisek k releasu.** Workflow použije jako popis releasu **zprávu
   anotovaného tagu** — proto piš tag message smysluplně (co se mění a proč).
6. **Nikdy neříkej „hotovo" bez ověření.** Minimálně zelené testy; u věcí, které
   se týkají nainstalovaného buildu, ověř na NAINSTALOVANÉ verzi (ne jen v dev),
   ideálně end-to-end. Co uživatel řekne „vyzkoušíme to", to opravdu vyzkoušej.

## Konvence kódu a architektury

- **Modely = reálné soubory, ne symlinky.** Vše kolem modelů jde přes
  `app/model_store.py` (jeden zdroj pravdy): každý model ve vlastní složce
  `models/<name>/`, stahuje se `download_model(output_dir=...)`, načítá přes cestu
  `WhisperModel("models/<name>")`. NEpoužívej výchozí HF blob/symlink cache —
  zabalený CTranslate2 symlinkovaný `model.bin` neotevře. `is_ready` musí
  kontrolovat reálný `model.bin` (ne symlink, rozumná velikost) + kompletní sadu
  souborů, ne „složka není prázdná".
- **Selhání ukazuj uživateli**, nepolykej je (status lišta + barva tray ikony +
  bublina). Stahování opakuj s backoffem (flaky síť, `WinError 10054`).
- **Modely nepatří do installeru** — stahují se za běhu (installer ať zůstane malý).
- **Žádné mazání jediné kopie modelu.** Migrace/úklid cache musí být transakční a
  nikdy nesmí smazat data, ze kterých nemáme ověřenou novou kopii.
- **Konce řádků** řeší `.gitattributes` (repo LF; `*.bat` CRLF) — necommituj
  CRLF/LF šum.

## Build / release specifika

- PyInstaller staví z `packaging/windows/MeetingNotetaker.spec`; spec je
  nezávislý na adresáři (cesty přes `SPECPATH`). Při buildu ze `.spec` NEpředávej
  „makespec-only" přepínače (např. `--paths`) — ISCC/PyInstaller je odmítne.
- Inno Setup: `packaging/windows/meeting-notetaker.iss`, výstup
  `packaging/windows/Output/MeetingNotetaker-Setup.exe`.

## Co dál hlídat (poučení z vývoje)

- **Testuj na platformě, kde bug je.** Některé chování (symlinky, frozen CT2)
  se projeví jen na Windows — nespoléhej, že to pokryje Linux CI.
- **Stahování modelů občas spadne na `WinError 10054`** (reset spojení na
  huggingface.co). NENÍ to prokazatelně antivir — spadlo to i pod čistým
  `python.exe`, takže jde o nahodilý síťový/TLS reset. Řeš to retry+backoffem a
  viditelným hlášením selhání, ne domněnkami o ESETu. (Code-signing exe je
  obecná hygiena, ne ověřená oprava tohohle.)
- **Ověřuj na nainstalovaném buildu**, ne jen v dev checkoutu — „funguje v dev"
  neznamená „funguje ve frozen buildu".
