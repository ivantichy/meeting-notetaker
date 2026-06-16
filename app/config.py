"""Konfigurace aplikace: AppConfig dataclass + load/save JSON."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields


@dataclass
class AppConfig:
    ics_url: str = ""
    language: str = "cs"
    live_model: str = "small"        # faster-whisper model name (živý přepis)
    post_model: str = "large-v3-turbo"  # finální dopřepsání po meetingu ("" = vypnuto)
    notes_dir: str = "notes"         # relative to app root or absolute
    poll_minutes: int = 5            # ICS refresh
    arm_window_s: int = 120          # arm N seconds before start
    stop_grace_s: int = 300          # keep recording N seconds past scheduled end
    chunk_seconds: int = 20          # audio chunk for live transcription
    sample_rate: int = 16000
    detect_calls: bool = True        # auto-detekce hovoru podle využití mikrofonu
    detect_stop_grace_s: int = 20    # zastavit detekovaný záznam N s po uvolnění mikrofonu
    early_stop_grace_s: int = 60     # ukončit kalendářový záznam N s po konci hovoru
    no_call_timeout_s: int = 180     # ukončit kalendářový záznam, když se hovor vůbec nerozběhne


def _backup_corrupt(path: str) -> None:
    """Přejmenuje poškozený config na ``<path>.corrupt``, aby se tajná ics_url
    tiše neztratila tím, že ji první spuštění přepíše výchozími hodnotami (M10).
    Selhání zálohy jen ignorujeme — načtení nesmí shodit aplikaci."""
    try:
        if os.path.exists(path):
            backup = path + ".corrupt"
            try:
                if os.path.exists(backup):
                    os.remove(backup)
            except OSError:
                pass
            os.replace(path, backup)
    except OSError:
        pass


def load_config(path: str) -> AppConfig:
    """Načte konfiguraci z JSON souboru; chybějící soubor/klíče -> výchozí hodnoty.

    Poškozený (nevalidní JSON nebo ne-objekt) config se nejdřív zazálohuje do
    ``<path>.corrupt`` — jinak by ho první spuštění přepsalo a tajná ics_url
    by se nenávratně ztratila (M10).
    """
    if not os.path.exists(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        return AppConfig()
    except ValueError:
        _backup_corrupt(path)
        return AppConfig()
    if not isinstance(data, dict):
        _backup_corrupt(path)
        return AppConfig()
    known = {f.name for f in fields(AppConfig)}
    return AppConfig(**{k: v for k, v in data.items() if k in known})


def save_config(cfg: AppConfig, path: str) -> None:
    """Uloží konfiguraci do JSON souboru (UTF-8, odsazené). Zápis je atomický
    (temp + rename), aby pád uprostřed nepoškodil stávající config."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    payload = json.dumps(asdict(cfg), indent=2, ensure_ascii=False) + "\n"
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
