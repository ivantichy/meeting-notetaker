"""Read-only MCP server exposing Ivan's local Czech meeting transcripts.

This is a Model Context Protocol (MCP) server (FastMCP, stdio transport) that
lets Claude — and any skill/task — query the Meeting Notetaker transcripts as
first-class tools, the same way a Granola or Fio-banka connector works.

Transcripts are STRICTLY READ-ONLY: the server lists, searches and reads notes,
and never writes, modifies or deletes any transcript. The ONLY mutating
capability is editing the transcription *glossary* (``glossary.txt``) — the
small list of names/tools/jargon that biases Whisper so they aren't
mis-transcribed; ``get_glossary`` / ``add_glossary_terms`` / ``remove_glossary_terms``
read and edit that one file. The data logic lives in plain functions (the
transcript ones take a ``notes_dir`` argument; the glossary ones take an explicit
``path``), easy to unit-test without a live stdio loop; the MCP tools are thin
wrappers that resolve the relevant path and call them.

Notes are markdown files (``notes/*.md``) with a YAML frontmatter header and a
``## Přepis`` section of ``[HH:MM:SS] …`` transcript lines; ``notes/index.jsonl``
is a machine-readable index (one JSON object per finished recording). The notes
directory is located via :func:`app.app_info.resolve_notes_dir` so the server
works for both the dev checkout and the installed build.

Run the dev server with::

    python -m app.mcp_server
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime

# resolve_notes_dir locates the transcripts for both dev and installed builds;
# resolve_glossary_path locates the single editable glossary.txt the same way.
from app.app_info import resolve_glossary_path, resolve_notes_dir

# Path-based glossary helpers (read + the ONLY mutating capability: edit the
# glossary). Aliased so the thin MCP wrappers can keep clean tool names without
# shadowing the underlying functions.
from app.glossary import (
    add_glossary_terms as _add_glossary_terms,
    read_glossary_terms as _read_glossary_terms,
    remove_glossary_terms as _remove_glossary_terms,
)

# Fields surfaced from a single index.jsonl record (in this order).
_INDEX_FIELDS = (
    "uid",
    "title",
    "platform",
    "event_start",
    "recorded_start",
    "duration_min",
    "note",
    "quality",
)


def _read_index(notes_dir: str) -> "list[dict]":
    """Read ``index.jsonl`` into a list of dicts (one per finished recording).

    Tolerant of a missing file and of malformed lines (those are skipped). Order
    matches the file (append order: oldest first).
    """
    path = os.path.join(notes_dir, "index.jsonl")
    out: "list[dict]" = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _index_sort_key(rec: dict) -> str:
    """Sort key for "newest first": recorded_start, else event_start, else ''."""
    val = rec.get("recorded_start") or rec.get("event_start") or ""
    return val if isinstance(val, str) else ""


def list_recent_meetings(notes_dir: str, limit: int = 20) -> "list[dict]":
    """Recent entries from ``index.jsonl``, newest first.

    Each entry carries: uid, title, platform, event_start, recorded_start,
    duration_min, note (the markdown filename) and quality. ``limit`` caps the
    number returned (values <= 0 mean "no limit").
    """
    records = _read_index(notes_dir)
    records.sort(key=_index_sort_key, reverse=True)
    if limit and limit > 0:
        records = records[:limit]
    result = []
    for rec in records:
        result.append({k: rec.get(k) for k in _INDEX_FIELDS})
    return result


def _title_from_markdown(text: str, fallback: str) -> str:
    """Best-effort note title: the frontmatter ``title:`` value, else the first
    ``# `` heading, else ``fallback`` (typically the filename without ``.md``)."""
    lines = text.split("\n")
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() == "---":
                break
            if line.startswith("title:"):
                value = line.partition(":")[2].strip()
                # Unwrap a double-quoted scalar (matches storage._fm_quote).
                if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
                    value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                if value:
                    return value
    for line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def _list_note_files(notes_dir: str) -> "list[str]":
    """Sorted list of ``*.md`` filenames in ``notes_dir`` (empty on any error)."""
    try:
        names = os.listdir(notes_dir)
    except OSError:
        return []
    return sorted(n for n in names if n.endswith(".md"))


def _make_snippet(text: str, pos: int, length: int, radius: int = 80) -> str:
    """A short one-line snippet of ``text`` around ``[pos, pos+length)``."""
    start = max(0, pos - radius)
    end = min(len(text), pos + length + radius)
    snippet = text[start:end].replace("\r", " ").replace("\n", " ").strip()
    snippet = " ".join(snippet.split())  # collapse runs of whitespace
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def search_transcripts(
    notes_dir: str, query: str, limit: int = 20
) -> "list[dict]":
    """Case-insensitive substring search across ``notes/*.md``.

    Returns one entry per matching note: note (filename), title and a short
    ``snippet`` taken around the FIRST hit in that note. ``limit`` caps the
    number of matching notes returned (values <= 0 mean "no limit"). An empty
    or whitespace-only query returns no results.
    """
    needle = (query or "").strip().lower()
    if not needle:
        return []
    results = []
    for name in _list_note_files(notes_dir):
        path = os.path.join(notes_dir, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        pos = text.lower().find(needle)
        if pos < 0:
            continue
        results.append(
            {
                "note": name,
                "title": _title_from_markdown(text, name[:-3]),
                "snippet": _make_snippet(text, pos, len(needle)),
            }
        )
        if limit and limit > 0 and len(results) >= limit:
            break
    return results


def _resolve_note_path(notes_dir: str, note: str) -> "str | None":
    """Safely resolve ``note`` to a ``*.md`` file *inside* ``notes_dir``.

    ``note`` may be either a note filename (``2026-06-15_1600_foo.md`` or just
    ``2026-06-15_1600_foo``) or a uid recorded in ``index.jsonl``. Security: the
    result is accepted ONLY when it is a regular ``.md`` file whose real path is
    contained within ``notes_dir`` — absolute paths, ``..`` traversal and any
    path that escapes the notes directory are rejected (returns ``None``).
    """
    if not note or not isinstance(note, str):
        return None
    note = note.strip()
    if not note:
        return None

    # If it's a known uid, map it to its note filename via the index first.
    candidate_name = note
    if "/" not in note and "\\" not in note:
        for rec in _read_index(notes_dir):
            if rec.get("uid") == note:
                mapped = rec.get("note")
                if isinstance(mapped, str) and mapped:
                    candidate_name = mapped
                break

    # Reject anything that is not a bare filename: no separators, no drive,
    # no parent-dir references. This blocks traversal and absolute paths
    # before we ever touch the filesystem.
    if (
        os.path.isabs(candidate_name)
        or "/" in candidate_name
        or "\\" in candidate_name
        or os.path.splitdrive(candidate_name)[0]
        or ".." in candidate_name.split(".")  # e.g. literal ".."
        or candidate_name in (".", "..")
    ):
        return None
    if not candidate_name.endswith(".md"):
        candidate_name = candidate_name + ".md"

    base = os.path.realpath(notes_dir)
    full = os.path.realpath(os.path.join(base, candidate_name))

    # Defence in depth: confirm the resolved path really sits inside notes_dir
    # (commonpath handles symlinks / odd inputs that slipped past the checks).
    try:
        if os.path.commonpath([base, full]) != base:
            return None
    except ValueError:
        # Different drives on Windows -> definitely outside.
        return None
    if not os.path.isfile(full):
        return None
    return full


def get_transcript(notes_dir: str, note: str) -> "str | None":
    """Full markdown of a single note, or ``None`` if it can't be resolved.

    ``note`` may be a filename or a uid; it is sanitised so only a ``*.md`` file
    *inside* ``notes_dir`` is ever read (no path traversal / absolute paths).
    """
    path = _resolve_note_path(notes_dir, note)
    if path is None:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _meeting_date(rec: dict) -> "date | None":
    """Local calendar date of a meeting from its recorded/event start."""
    raw = rec.get("recorded_start") or rec.get("event_start")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone()  # to local time before taking the date
    return dt.date()


def get_today(notes_dir: str, today: "date | None" = None) -> "list[dict]":
    """Today's meetings from the index (newest first); ``today`` is for tests."""
    if today is None:
        today = date.today()
    out = []
    for rec in list_recent_meetings(notes_dir, limit=0):
        d = _meeting_date(rec)
        if d is not None and d == today:
            out.append(rec)
    return out


# --------------------------------------------------------------------------- #
# MCP server (FastMCP, stdio). The tools below are thin wrappers that resolve   #
# the notes directory and delegate to the pure functions above.                #
# --------------------------------------------------------------------------- #
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("meeting-notetaker")


def _json(data) -> str:
    """Serialise a tool result to pretty UTF-8 JSON (readable Czech text)."""
    return json.dumps(data, ensure_ascii=False, indent=2)


@mcp.tool(name="list_recent_meetings")
def list_recent_meetings_tool(limit: int = 20) -> str:
    """List Ivan's most recent local meetings, newest first.

    Serves Ivan's local Czech meeting transcripts (the Meeting Notetaker app).
    Route any question about his recent meetings, calls, or recordings here.
    Returns a JSON array of entries with: uid, title, platform, event_start,
    recorded_start, duration_min, note (markdown filename) and quality. Pass the
    returned ``note`` (or ``uid``) to ``get_transcript`` to read the full text.

    Args:
        limit: Maximum number of meetings to return (default 20, newest first).
    """
    notes_dir = resolve_notes_dir()
    return _json(list_recent_meetings(notes_dir, limit=limit))


@mcp.tool(name="search_transcripts")
def search_transcripts_tool(query: str, limit: int = 20) -> str:
    """Full-text search across Ivan's local Czech meeting transcripts.

    Case-insensitive substring search over every meeting note (the Meeting
    Notetaker app). Use this to find what was said or decided in Ivan's calls —
    e.g. a person's name, a project, a decision or an action item. Returns a
    JSON array of matches, each with: note (markdown filename), title and a
    short snippet around the hit. Pass a match's ``note`` to ``get_transcript``
    for the full conversation.

    Args:
        query: Text to search for (case-insensitive substring).
        limit: Maximum number of matching notes to return (default 20).
    """
    notes_dir = resolve_notes_dir()
    return _json(search_transcripts(notes_dir, query, limit=limit))


@mcp.tool(name="get_transcript")
def get_transcript_tool(note: str) -> str:
    """Return the full markdown of one of Ivan's local meeting transcripts.

    Reads a single note from the Meeting Notetaker app — frontmatter header
    (title, time, attendees, platform) plus the ``## Přepis`` transcript with
    ``[HH:MM:SS]`` lines. ``note`` may be a filename (from ``list_recent_meetings``
    / ``search_transcripts``) or a meeting ``uid``. Read-only and sandboxed: only
    a note inside Ivan's notes folder can be read. Returns an error string if the
    note can't be found or is outside the notes folder.

    Args:
        note: Note filename (e.g. ``2026-06-15_1600_foo.md``) or meeting uid.
    """
    notes_dir = resolve_notes_dir()
    content = get_transcript(notes_dir, note)
    if content is None:
        return f"Note not found (or outside the notes folder): {note!r}"
    return content


@mcp.tool(name="get_today")
def get_today_tool() -> str:
    """List Ivan's meetings recorded today, newest first.

    Convenience view over Ivan's local Czech meeting transcripts (the Meeting
    Notetaker app): same entry shape as ``list_recent_meetings`` but limited to
    today's date. Returns a JSON array (empty if nothing was recorded today).
    """
    notes_dir = resolve_notes_dir()
    return _json(get_today(notes_dir))


# --------------------------------------------------------------------------- #
# Glossary tools — the ONLY writable surface. They manage Ivan's transcription  #
# glossary (names/tools/jargon Whisper would otherwise mis-transcribe). Each    #
# resolves the single editable glossary.txt and calls a path-based helper.      #
# Transcripts/notes stay strictly read-only.                                    #
# --------------------------------------------------------------------------- #


@mcp.tool(name="get_glossary")
def get_glossary_tool() -> str:
    """Return Ivan's current transcription glossary (his custom vocabulary).

    The glossary is a small list of names, tool/product names and jargon that
    biases the Meeting Notetaker's Czech transcription so those words aren't
    mis-transcribed (e.g. ``elem6``, ``Claude``, ``Kubernetes``). It is the only
    editable surface of this server — transcripts themselves are read-only.
    Keep it small — it feeds a ~224-token Whisper prompt, so a bloated list gets
    truncated and can crowd out the per-meeting attendee names.
    Returns a JSON object ``{"glossary": [...]}`` with the terms in file order.
    Use ``add_glossary_terms`` / ``remove_glossary_terms`` to change it.
    """
    path = resolve_glossary_path()
    return _json({"glossary": _read_glossary_terms(path)})


@mcp.tool(name="add_glossary_terms")
def add_glossary_terms_tool(terms: "list[str]") -> str:
    """Add terms to Ivan's transcription glossary (names/tools/jargon).

    Use this to teach the Meeting Notetaker words it keeps getting wrong — e.g. a
    colleague's name, a product or a piece of jargon — so future recordings
    transcribe them correctly. Terms already present (case-insensitively) are
    skipped; the glossary file's comments and ordering are preserved. The change
    applies to the NEXT transcription (no restart needed). This glossary is the
    only thing this server can edit — meeting transcripts/notes stay read-only.
    Keep the glossary small and high-value — it feeds a ~224-token Whisper
    prompt, so a bloated list gets truncated and can crowd out the per-meeting
    attendee names; add only terms that are actually mis-transcribed, not bulk
    vocabulary. Returns JSON ``{"added": [...], "glossary": [...]}`` (the terms
    newly added and the resulting full glossary).

    Args:
        terms: Words/phrases to add (e.g. ["elem6", "Kubernetes"]).
    """
    path = resolve_glossary_path()
    before = {t.casefold() for t in _read_glossary_terms(path)}
    glossary = _add_glossary_terms(path, terms or [])
    added = [t for t in glossary if t.casefold() not in before]
    return _json({"added": added, "glossary": glossary})


@mcp.tool(name="remove_glossary_terms")
def remove_glossary_terms_tool(terms: "list[str]") -> str:
    """Remove terms from Ivan's transcription glossary.

    Use this to drop words that no longer belong in the Meeting Notetaker's
    custom vocabulary (the names/tools/jargon that bias Czech transcription).
    Matching is case-insensitive; comments and other terms are kept and the file
    structure is preserved. The change applies to the NEXT transcription. This
    glossary is the only thing this server can edit — meeting transcripts/notes
    stay strictly read-only. Returns JSON ``{"removed": [...], "glossary": [...]}``
    (the terms actually removed and the resulting full glossary).

    Args:
        terms: Words/phrases to remove (case-insensitive match).
    """
    path = resolve_glossary_path()
    before = _read_glossary_terms(path)
    before_keys = {t.casefold() for t in before}
    glossary = _remove_glossary_terms(path, terms or [])
    after_keys = {t.casefold() for t in glossary}
    removed = [t for t in before if t.casefold() in before_keys - after_keys]
    return _json({"removed": removed, "glossary": glossary})


def main() -> None:
    """Entry point: run the stdio MCP server (blocks until the client closes)."""
    mcp.run()


if __name__ == "__main__":
    main()
