"""Ukládání poznámek: markdown soubory s YAML frontmatter + průběžný přepis.

Frontmatter se zapisuje i parsuje ručně (jednoduchý, robustní formát) — žádná
externí YAML knihovna není potřeba.
"""
from __future__ import annotations

import os
from datetime import datetime

from app.models import Meeting

CONTINUATION_MARKER = "\n\n--- pokračování záznamu ---\n"


def _format_timestamp(seconds: float) -> str:
    """Sekundy od začátku záznamu -> 'HH:MM:SS'."""
    total = int(seconds)
    if total < 0:
        total = 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


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
            meta[current_list_key].append(stripped[2:].strip())
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if value == "" or value == "[]":
                meta[key] = [] if key == "attendees" else value
                current_list_key = key if value == "" else None
                if value == "" and key == "attendees":
                    meta[key] = []
            else:
                meta[key] = value
                current_list_key = None
    return meta


class NoteStore:
    def __init__(self, notes_dir: str):
        self.notes_dir = notes_dir
        os.makedirs(notes_dir, exist_ok=True)
        import threading

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
        import json

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
        import json

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
            with open(self.index_path, "w", encoding="utf-8") as f:
                f.writelines(out)

    def _frontmatter(self, meeting: Meeting) -> str:
        lines = [
            "---",
            f"title: {meeting.title}",
            f"start: {meeting.start.isoformat()}",
            f"end: {meeting.end.isoformat()}",
            f"platform: {meeting.platform.value}",
        ]
        if meeting.attendees:
            lines.append("attendees:")
            lines.extend(f"  - {a}" for a in meeting.attendees)
        else:
            lines.append("attendees: []")
        lines.append(f"join_url: {meeting.join_url or ''}")
        lines.append("status: recording")
        lines.append("---")
        return "\n".join(lines)

    def create_note(self, meeting: Meeting) -> str:
        """Vytvoří notes/<slug>.md s frontmatter + nadpisem '## Přepis'. Vrací cestu.

        Pokud soubor existuje (restart aplikace uprostřed schůzky), připojí
        místo toho značku pokračování.
        """
        path = os.path.join(self.notes_dir, f"{meeting.slug}.md")
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
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(new_lines))

    def replace_transcript(
        self, path: str, segments: "list[tuple[float, float, str]]"
    ) -> None:
        """Nahradí obsah sekce '## Přepis' novými řádky '[HH:MM:SS] text'
        (t0 v sekundách od začátku záznamu, stejný formát jako append_segment).

        Vše za řádkem '## Přepis' se zahodí (včetně '--- pokračování záznamu ---'
        markerů) a nahradí novými řádky. Frontmatter a hlavička zůstávají,
        ale do frontmatteru se přidá/aktualizuje klíč 'transcript_quality: final'
        (vkládá se za řádek 'status:' pokud klíč ještě neexistuje).
        """
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        lines = text.split("\n")

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
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(head + seg_lines) + "\n")

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
