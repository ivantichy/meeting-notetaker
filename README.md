# Meeting Notetaker

Lokální zapisovatel schůzek pro Windows — bez bota. Nahrává zvuk systému
(reproduktory přes WASAPI loopback + mikrofon), přepisuje česky pomocí
faster-whisper přímo na vašem počítači a ukládá poznámky jako Markdown soubory.
Schůzky čte z Google Kalendáře přes tajnou ICS adresu a záznam spouští
i ukončuje automaticky.

## Instalace

```bat
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
run.bat
```

Při prvním spuštění se aplikace zeptá na tajnou ICS adresu kalendáře
a stáhne model Whisper (chvíli to trvá; model se uloží do složky `models/`).

## Jak získat tajnou ICS adresu

1. Otevřete [Google Kalendář](https://calendar.google.com) v prohlížeči.
2. **Nastavení** (ozubené kolo) → **Nastavení mého kalendáře** → vyberte svůj kalendář.
3. Sekce **Integrovat kalendář** → zkopírujte **„Tajná adresa ve formátu iCal“**.
4. Adresu vložte do dialogu při prvním spuštění (nebo do `config.json`, klíč `ics_url`).

Adresu nikomu nedávejte — kdokoli s ní vidí váš kalendář.

## Kde jsou poznámky

Ve složce `notes/` vedle aplikace, jeden soubor na schůzku, např.
`notes/2026-06-12_1330_porada-tymu.md`. Obsahuje hlavičku (název, čas,
účastníci, platforma) a přepis s časovými značkami `[HH:MM:SS]`.

## Jak funguje auto-záznam

- Aplikace každých pár minut načte kalendář (interval `poll_minutes`).
- 2 minuty před začátkem schůzky na Meetu/Teams se „připraví“ (zvýraznění
  v seznamu + odpočet v pravém panelu).
- V čase začátku schůzky se záznam spustí sám; ukončí se automaticky
  5 minut po plánovaném konci (`stop_grace_s`).
- Kdykoli lze nahrávat ručně tlačítkem **● Nahrát teď** a zastavit
  tlačítkem **■ Zastavit záznam**.
- Zavření okna aplikaci neukončí — běží dál v oznamovací oblasti (tray).
  Ukončíte ji přes pravé tlačítko na ikoně → **Ukončit**.

## Soukromí

Vše běží **lokálně** — zvuk ani přepis neopouští váš počítač, žádná cloudová
služba se nepoužívá. Pozor však: na rozdíl od meeting-botů **účastníci hovoru
nevidí, že se nahrává**. Je slušností (a podle situace i právní povinností)
je o nahrávání předem informovat.
