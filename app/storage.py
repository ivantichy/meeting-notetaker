"""Ukládání poznámek: markdown soubory s YAML frontmatter + průběžný přepis.

Frontmatter se zapisuje i parsuje ručně (jednoduchý, robustní formát) — žádná
externí YAML knihovna není potřeba.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from app.models import Meeting

CONTINUATION_MARKER = "\n\n--- pokračování záznamu ---\n"


def _atomic_write(path: str, content: str) -> None:
    """Zapíše soubor atomicky: nejdřív do dočasného souboru ve stejném adresáři,
    pak ``os.replace`` (atomický rename). Pád uprostřed nepoškodí původní soubor."""
    directory = os.path.dirname(os.path.abspath(path))
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _format_timestamp(seconds: float) -> str:
    """Sekundy od začátku záznamu -> 'HH:MM:SS'."""
    total = int(seconds)
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


#: Znaky/sekvence, kvůli kterým není hodnota platná „plain scalar" v YAML a je
#: nutné ji uvozovkovat (jinak by se rozbil ruční i běžný YAML parser).
def _fm_needs_quote(value: str) -> bool:
    if value == "":
        return False  # prázdná hodnota je platná (klíč bez hodnoty / blok listu)
    if value != value.strip():
        return True  # vedoucí/koncové mezery
    if value[0] in "-?:,[]{}#&*!|>'\"%@`":
        return True  # vedoucí indikátor (mapování, sekvence, komentář, …)
    if ": " in value or value.endswith(":"):
        return True  # vypadá jako vnořené mapování -> rozbije parser
    if " #" in value:
        return True  # začátek komentáře
    return False


def _fm_quote(value: str) -> str:
    """Bezpečně serializuje hodnotu do frontmatteru: odstraní zalomení řádků
    a podle potřeby uvozovkuje (double-quoted YAML scalar s escapováním)."""
    # Zalomení řádků by jinak ukončila hodnotu/frontmatter nebo vložila klíče.
    value = value.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    if _fm_needs_quote(value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _fm_unquote(value: str) -> str:
    """Inverze k _fm_quote: rozbalí double-quoted hodnotu (escape \\\\, \\\")."""
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        inner = value[1:-1]
        out = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                out.append('"' if nxt == '"' else "\\" if nxt == "\\" else nxt)
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return value


#: Klíče frontmatteru, jejichž hodnota je seznam (řádky '- …'). Prázdná hodnota
#: nebo '[]' u nich znamená prázdný seznam (ne prázdný řetězec).
_FM_LIST_KEYS = frozenset({"attendees", "attendee_names", "topic_terms"})


def _parse_frontmatter(text: str) -> dict:
    """Naparsuje jednoduchý YAML frontmatter na dict (listy = klíč s '- ' řádky)."""
    meta: dict = {}
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return meta
    current_list_key = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        if stripped.startswith("- ") and current_list_key is not None:
            meta[current_list_key].append(_fm_unquote(stripped[2:].strip()))
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "" or value == "[]":
                is_list_key = key in _FM_LIST_KEYS
                meta[key] = [] if is_list_key else value
                # Po prázdné hodnotě listového klíče čteme následující '- ' řádky.
                current_list_key = key if (value == "" and is_list_key) else None
            else:
                meta[key] = _fm_unquote(value)
                current_list_key = None
    return meta


class NoteStore:
    def __init__(self, notes_dir: str):
        self.notes_dir = notes_dir
        os.makedirs(notes_dir, exist_ok=True)
        self._index_lock = threading.Lock()

    # ------------------------------------------------------------- index
    # notes/index.jsonl: strojově čitelný seznam záznamů (1 JSON na řádek):
    # {"uid","title","platform","event_start","recorded_start",
    #  "duration_min","note","quality"}

    @property
    def index_path(self) -> str:
        return os.path.join(self.notes_dir, "index.jsonl")

    def index_add(
        self,
        meeting: Meeting,
        note_path: str,
        duration_s: float,
        recorded_start: str,
    ) -> None:
        """Připíše dokončený záznam do index.jsonl (quality: live)."""
        rec = {
            "uid": meeting.uid,
            "title": meeting.title,
            "platform": meeting.platform.value,
            "event_start": meeting.start.isoformat(),
            "recorded_start": recorded_start,
            "duration_min": round(duration_s / 60.0, 1),
            "note": os.path.basename(note_path),
            "quality": "live",
        }
        with self._index_lock:
            with open(self.index_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def index_mark_final(self, note_path: str) -> None:
        """Po dopřepsání turbo modelem přepne quality záznamu na 'final'."""
        name = os.path.basename(note_path)
        with self._index_lock:
            if not os.path.exists(self.index_path):
                return
            with open(self.index_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            out = []
            for line in lines:
                try:
                    rec = json.loads(line)
                except ValueError:
                    out.append(line)
                    continue
                if rec.get("note") == name:
                    rec["quality"] = "final"
                    out.append(json.dumps(rec, ensure_ascii=False) + "\n")
                else:
                    out.append(line)
            _atomic_write(self.index_path, "".join(out))

    def _frontmatter(self, meeting: Meeting) -> str:
        lines = [
            "---",
            f"uid: {_fm_quote(meeting.uid)}",
            f"title: {_fm_quote(meeting.title)}",
            f"start: {meeting.start.isoformat()}",
            f"end: {meeting.end.isoformat()}",
            f"platform: {meeting.platform.value}",
        ]
        if meeting.attendees:
            lines.append("attendees:")
            lines.extend(f"  - {_fm_quote(a)}" for a in meeting.attendees)
        else:
            lines.append("attendees: []")
        # Zobrazovaná jména (CN z kalendáře) — additivní klíč pro initial_prompt
        # slovník. Staré poznámky ho nemají; parser to snese (vrátí []).
        names = getattr(meeting, "attendee_names", None) or []
        if names:
            lines.append("attendee_names:")
            lines.extend(f"  - {_fm_quote(n)}" for n in names)
        else:
            lines.append("attendee_names: []")
        # Tematické termíny vytěžené z názvu + popisu schůzky (additivní klíč).
        # Počítáme je TADY (při zápisu poznámky), ať je finální přepis dostane
        # z frontmatteru i bez kalendáře. Lokální, deterministické, bez LLM.
        # Staré poznámky klíč nemají; parser to snese (vrátí []).
        from app.glossary import extract_topic_terms

        topic_terms = extract_topic_terms(
            meeting.title, getattr(meeting, "description", "") or ""
        )
        if topic_terms:
            lines.append("topic_terms:")
            lines.extend(f"  - {_fm_quote(t)}" for t in topic_terms)
        else:
            lines.append("topic_terms: []")
        lines.append(f"join_url: {_fm_quote(meeting.join_url or '')}")
        lines.append("status: recording")
        lines.append("---")
        return "\n".join(lines)

    def _is_same_meeting(self, path: str, meeting: Meeting) -> bool:
        """True, pokud existující poznámka patří téže schůzce (shoduje se uid,
        nebo — u starších poznámek bez uid — start). Slouží k rozlišení
        restartu téže schůzky od kolize slugů dvou různých schůzek."""
        try:
            with open(path, "r", encoding="utf-8") as f:
                meta = _parse_frontmatter(f.read())
        except OSError:
            return True  # nečitelné -> konzervativně považuj za totéž (zachová staré chování)
        uid = meta.get("uid")
        if isinstance(uid, str) and uid:
            return uid == meeting.uid
        start = meta.get("start")
        if isinstance(start, str) and start:
            return start == meeting.start.isoformat()
        return True  # bez identifikátoru (starý formát) -> zachovat původní chování

    def _dedup_path(self, meeting: Meeting) -> str:
        """Vrátí cestu pro slug; při kolizi s JINOU schůzkou přidá sufix _2, _3, …
        Pokud existující soubor patří téže schůzce, vrátí ho beze změny (restart)."""
        base = os.path.join(self.notes_dir, f"{meeting.slug}.md")
        if not os.path.exists(base) or self._is_same_meeting(base, meeting):
            return base
        i = 2
        while True:
            cand = os.path.join(self.notes_dir, f"{meeting.slug}_{i}.md")
            if not os.path.exists(cand) or self._is_same_meeting(cand, meeting):
                return cand
            i += 1

    def create_note(self, meeting: Meeting) -> str:
        """Vytvoří notes/<slug>.md s frontmatter + nadpisem '## Přepis'. Vrací cestu.

        Pokud soubor existuje a patří TÉŽE schůzce (restart aplikace uprostřed
        schůzky), připojí místo toho značku pokračování. Pokud slug koliduje
        s JINOU schůzkou (stejná minuta + název), použije se sufix _2, _3, …
        """
        path = self._dedup_path(meeting)
        if os.path.exists(path):
            with open(path, "a", encoding="utf-8") as f:
                f.write(CONTINUATION_MARKER)
            return path
        content = (
            self._frontmatter(meeting)
            + "\n\n"
            + f"# {meeting.title}\n\n"
            + "## Přepis\n"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path

    def append_segment(self, path: str, t0: float, t1: float, text: str) -> None:
        """Připojí řádek '[HH:MM:SS] text' (t = sekundy od začátku záznamu)."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{_format_timestamp(t0)}] {text.strip()}\n")

    def finalize(self, path: str, duration_s: float) -> None:
        """Přepíše ve frontmatter status: recording -> done a doplní duration_min."""
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            return
        duration_min = round(duration_s / 60)
        new_lines = []
        closed = False
        has_duration = False
        for i, line in enumerate(lines):
            if i == 0:
                new_lines.append(line)
                continue
            if not closed:
                if line.strip() == "---":
                    if not has_duration:
                        new_lines.append(f"duration_min: {duration_min}")
                    new_lines.append(line)
                    closed = True
                    continue
                if line.startswith("status:"):
                    new_lines.append("status: done")
                    continue
                if line.startswith("duration_min:"):
                    new_lines.append(f"duration_min: {duration_min}")
                    has_duration = True
                    continue
            new_lines.append(line)
        _atomic_write(path, "\n".join(new_lines))

    def replace_transcript(
        self, path: str, segments: "list[tuple[float, float, str]]"
    ) -> bool:
        """Nahradí obsah sekce '## Přepis' novými řádky '[HH:MM:SS] text'
        (t0 v sekundách od začátku záznamu, stejný formát jako append_segment).

        Vše za řádkem '## Přepis' se zahodí (včetně '--- pokračování záznamu ---'
        markerů) a nahradí novými řádky. Frontmatter a hlavička zůstávají,
        ale do frontmatteru se přidá/aktualizuje klíč 'transcript_quality: final'
        (vkládá se za řádek 'status:' pokud klíč ještě neexistuje).

        Pojistka proti ztrátě dat (M5): pokud je nový přepis prázdný nebo
        výrazně kratší než stávající (živý) přepis, nahrazení se PŘESKOČÍ a
        vrátí se ``False`` (volající pak nesmí smazat zdrojový WAV). Jinak
        vrací ``True``. Zápis je atomický (temp + rename).
        """
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")

        # 0) pojistka: nový přepis nesmí zahodit lepší stávající (živý) přepis
        new_text_len = sum(len((s[2] or "").strip()) for s in segments)
        if new_text_len == 0:
            return False  # prázdný finální přepis -> nech živý a zachovej WAV
        old_text_len = self._transcript_text_len(lines)
        if old_text_len > 0 and new_text_len < old_text_len * 0.5:
            return False  # finální výrazně kratší -> podezřelý, nech živý a WAV

        # 1) frontmatter: přidat/aktualizovat transcript_quality: final
        if lines and lines[0].strip() == "---":
            fm_end = None
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    fm_end = i
                    break
            if fm_end is not None:
                has_quality = any(
                    l.startswith("transcript_quality:") for l in lines[1:fm_end]
                )
                new_fm = []
                for line in lines[1:fm_end]:
                    if line.startswith("transcript_quality:"):
                        new_fm.append("transcript_quality: final")
                        continue
                    new_fm.append(line)
                    if not has_quality and line.startswith("status:"):
                        new_fm.append("transcript_quality: final")
                        has_quality = True
                lines = [lines[0]] + new_fm + lines[fm_end:]

        # 2) najít řádek '## Přepis' a vše za ním zahodit
        heading_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "## Přepis":
                heading_idx = i
                break
        if heading_idx is None:
            # hlavička chybí (poškozený soubor) — doplnit na konec
            while lines and lines[-1] == "":
                lines.pop()
            lines.extend(["", "## Přepis"])
            heading_idx = len(lines) - 1

        head = lines[: heading_idx + 1]
        seg_lines = []
        for seg in segments:
            t0, seg_text = seg[0], seg[2]
            speaker = seg[3] if len(seg) > 3 and seg[3] else None
            prefix = f"{speaker}: " if speaker else ""
            seg_lines.append(f"[{_format_timestamp(t0)}] {prefix}{seg_text.strip()}")
        _atomic_write(path, "\n".join(head + seg_lines) + "\n")
        return True

    @staticmethod
    def _transcript_text_len(lines: "list[str]") -> int:
        """Délka textu stávajícího přepisu (řádky '[HH:MM:SS] …' za '## Přepis'),
        bez časových značek a jmen mluvčích — slouží k porovnání kvality (M5)."""
        import re

        total = 0
        in_section = False
        for line in lines:
            if line.strip() == "## Přepis":
                in_section = True
                continue
            if not in_section:
                continue
            m = re.match(r"^\[\d\d:\d\d:\d\d\]\s*(.*)$", line)
            if not m:
                continue
            body = m.group(1)
            if ": " in body:  # odstranit prefix "Mluvčí: "
                body = body.split(": ", 1)[1]
            total += len(body.strip())
        return total

    def list_notes(self) -> "list[dict]":
        """Vrátí [{path,title,start,status}] pro *.md, seřazené dle start sestupně."""
        notes = []
        try:
            names = os.listdir(self.notes_dir)
        except OSError:
            return []
        for name in sorted(names):
            if not name.endswith(".md"):
                continue
            path = os.path.join(self.notes_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            meta = _parse_frontmatter(text)
            start_raw = meta.get("start", "")
            start = None
            if isinstance(start_raw, str) and start_raw:
                try:
                    start = datetime.fromisoformat(start_raw)
                except ValueError:
                    start = None
            notes.append(
                {
                    "path": path,
                    "title": meta.get("title", name[:-3]),
                    "start": start if start is not None else start_raw,
                    "status": meta.get("status", ""),
                }
            )
        def _sort_key(n):
            s = n["start"]
            if isinstance(s, datetime):
                return s.isoformat() if s.tzinfo is None else s.astimezone().isoformat()
            return str(s)
        notes.sort(key=_sort_key, reverse=True)
        return notes
