"""Testy ukládání poznámek (NoteStore) a slugů."""
import os
from datetime import datetime, timedelta

import pytest
from dateutil import tz

from app.models import Meeting, Platform
from app.storage import NoteStore


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestSlug:
    def test_slug_format(self, sample_meeting):
        # fixed_now = 2026-06-12 13:00 Europe/Prague
        assert sample_meeting.slug == "2026-06-12_1300_tymova-porada-c-5"

    def test_slug_is_ascii_kebab(self, make_meeting, fixed_now):
        m = make_meeting(fixed_now, title="Žluťoučký kůň — příliš ďábelské Č!")
        slug = m.slug
        assert slug.isascii()
        assert " " not in slug
        assert slug == "2026-06-12_1300_zlutoucky-kun-prilis-dabelske-c"

    def test_slug_max_60_chars(self, make_meeting, fixed_now):
        m = make_meeting(fixed_now, title="Velmi dlouhý název schůzky " * 5)
        assert len(m.slug) <= 60
        assert m.slug.startswith("2026-06-12_1300_velmi-dlouhy-nazev")
        assert not m.slug.endswith("-")

    def test_slug_empty_title_fallback(self, make_meeting, fixed_now):
        m = make_meeting(fixed_now, title="???")
        assert m.slug == "2026-06-12_1300_meeting"


class TestCreateNote:
    def test_creates_file_and_returns_path(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        assert os.path.isfile(path)
        assert path == os.path.join(tmp_notes_dir, sample_meeting.slug + ".md")

    def test_frontmatter_content(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        text = _read(store.create_note(sample_meeting))
        lines = text.split("\n")
        assert lines[0] == "---"
        assert f"title: {sample_meeting.title}" in lines
        assert f"start: {sample_meeting.start.isoformat()}" in lines
        assert f"end: {sample_meeting.end.isoformat()}" in lines
        assert "platform: meet" in lines
        assert "attendees:" in lines
        assert "  - ivan@example.com" in lines
        assert "  - petr@example.com" in lines
        assert f"join_url: {sample_meeting.join_url}" in lines
        assert "status: recording" in lines
        # frontmatter je uzavřený druhým '---'
        assert lines.index("---", 1) > 1

    def test_body_structure(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        text = _read(store.create_note(sample_meeting))
        body = text.split("\n---\n", 1)[1]
        assert body.startswith("\n# Týmová porada č. 5\n\n## Přepis\n")

    def test_empty_attendees_as_yaml_empty_list(self, tmp_notes_dir, make_meeting, fixed_now):
        store = NoteStore(tmp_notes_dir)
        m = make_meeting(fixed_now, attendees=[])
        text = _read(store.create_note(m))
        assert "attendees: []" in text

    def test_restart_appends_continuation_marker(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path1 = store.create_note(sample_meeting)
        store.append_segment(path1, 0.0, 5.0, "První část.")
        path2 = store.create_note(sample_meeting)
        assert path2 == path1
        text = _read(path1)
        assert "--- pokračování záznamu ---" in text
        # frontmatter se nesmí zduplikovat
        assert text.count("status: recording") == 1
        assert text.count("# Týmová porada č. 5") == 1
        # marker je až za původním obsahem
        assert text.index("První část.") < text.index("pokračování záznamu")


class TestAppendSegment:
    def test_appends_timestamped_line(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.append_segment(path, 5.2, 9.8, "Dobrý den všem.")
        assert "[00:00:05] Dobrý den všem.\n" in _read(path)

    def test_hours_minutes_seconds_format(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.append_segment(path, 3661.0, 3665.0, "Hodina a kousek.")
        store.append_segment(path, 0.0, 2.0, "Začátek.")
        text = _read(path)
        assert "[01:01:01] Hodina a kousek." in text
        assert "[00:00:00] Začátek." in text

    def test_segments_in_order_after_prepis_heading(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.append_segment(path, 1.0, 3.0, "Jedna.")
        store.append_segment(path, 4.0, 6.0, "Dva.")
        text = _read(path)
        assert text.index("## Přepis") < text.index("[00:00:01] Jedna.") < text.index(
            "[00:00:04] Dva."
        )


class TestFinalize:
    def test_status_done_and_duration(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.append_segment(path, 0.0, 2.0, "Ahoj.")
        store.finalize(path, duration_s=3725.0)   # 62.08 min -> 62
        text = _read(path)
        assert "status: done" in text
        assert "status: recording" not in text
        assert "duration_min: 62" in text

    def test_duration_rounding(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.finalize(path, duration_s=89.0)     # 1.48 min -> 1
        assert "duration_min: 1" in _read(path)

    def test_body_untouched(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.append_segment(path, 0.0, 2.0, "Důležitá věta.")
        before_body = _read(path).split("\n---\n", 1)[1]
        store.finalize(path, duration_s=120.0)
        after = _read(path)
        assert after.split("\n---\n", 1)[1] == before_body
        # duration_min je uvnitř frontmatter, ne v těle
        frontmatter = after.split("\n---\n", 1)[0]
        assert "duration_min: 2" in frontmatter

    def test_double_finalize_keeps_single_duration(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        path = store.create_note(sample_meeting)
        store.finalize(path, duration_s=60.0)
        store.finalize(path, duration_s=180.0)
        text = _read(path)
        assert text.count("duration_min:") == 1
        assert "duration_min: 3" in text
        assert text.count("status:") == 1


class TestListNotes:
    def test_lists_sorted_by_start_desc(self, tmp_notes_dir, make_meeting, fixed_now):
        store = NoteStore(tmp_notes_dir)
        older = make_meeting(fixed_now - timedelta(days=1), title="Starší schůzka")
        newer = make_meeting(fixed_now, title="Novější schůzka")
        p_old = store.create_note(older)
        p_new = store.create_note(newer)
        store.finalize(p_old, duration_s=600.0)

        notes = store.list_notes()
        assert len(notes) == 2
        assert [n["title"] for n in notes] == ["Novější schůzka", "Starší schůzka"]
        assert notes[0]["path"] == p_new
        assert notes[0]["status"] == "recording"
        assert notes[1]["status"] == "done"
        assert notes[0]["start"] == fixed_now

    def test_dict_keys(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        store.create_note(sample_meeting)
        (note,) = store.list_notes()
        assert set(note.keys()) == {"path", "title", "start", "status"}

    def test_empty_dir(self, tmp_notes_dir):
        store = NoteStore(tmp_notes_dir)
        assert store.list_notes() == []

    def test_ignores_non_md_files(self, tmp_notes_dir, sample_meeting):
        store = NoteStore(tmp_notes_dir)
        store.create_note(sample_meeting)
        with open(os.path.join(tmp_notes_dir, "poznamka.txt"), "w", encoding="utf-8") as f:
            f.write("nic")
        assert len(store.list_notes()) == 1


def test_replace_transcript_replaces_lines_and_continuation_markers(
    tmp_notes_dir, sample_meeting
):
    store = NoteStore(tmp_notes_dir)
    path = store.create_note(sample_meeting)
    store.append_segment(path, 0.0, 2.0, "Živý odhad jedna.")
    store.create_note(sample_meeting)  # restart -> '--- pokračování záznamu ---'
    store.append_segment(path, 10.0, 12.0, "Živý odhad po restartu.")

    store.replace_transcript(
        path,
        [(0.0, 3.0, "Finální věta."), (3661.0, 3665.0, "Hodina a kousek.")],
    )
    text = _read(path)
    # staré řádky i marker pokračování pryč
    assert "Živý odhad jedna." not in text
    assert "Živý odhad po restartu." not in text
    assert "pokračování záznamu" not in text
    # nové řádky ve formátu [HH:MM:SS] za nadpisem '## Přepis'
    assert "[00:00:00] Finální věta." in text
    assert "[01:01:01] Hodina a kousek." in text
    assert text.index("## Přepis") < text.index("[00:00:00] Finální věta.") < text.index(
        "[01:01:01] Hodina a kousek."
    )
    # frontmatter a hlavička zůstávají
    assert text.startswith("---\n")
    assert f"title: {sample_meeting.title}" in text
    assert "status: recording" in text
    assert "# Týmová porada č. 5" in text


def test_replace_transcript_adds_quality_key_after_status(tmp_notes_dir, sample_meeting):
    store = NoteStore(tmp_notes_dir)
    path = store.create_note(sample_meeting)
    store.replace_transcript(path, [(0.0, 1.0, "Věta.")])
    lines = _read(path).split("\n")
    i_status = lines.index("status: recording")
    assert lines[i_status + 1] == "transcript_quality: final"
    # klíč je uvnitř frontmatteru (před uzavíracím '---')
    assert i_status + 1 < lines.index("---", 1)


def test_replace_transcript_idempotent_no_duplicate_key(tmp_notes_dir, sample_meeting):
    store = NoteStore(tmp_notes_dir)
    path = store.create_note(sample_meeting)
    store.replace_transcript(path, [(0.0, 1.0, "První běh.")])
    store.replace_transcript(path, [(0.0, 1.0, "Druhý běh.")])
    text = _read(path)
    assert text.count("transcript_quality:") == 1
    assert "transcript_quality: final" in text
    assert "Druhý běh." in text
    assert "První běh." not in text


# ----------------------------------------------------------------- index.jsonl


def test_index_add_writes_jsonl(tmp_notes_dir, sample_meeting):
    import json

    store = NoteStore(tmp_notes_dir)
    path = store.create_note(sample_meeting)
    store.index_add(sample_meeting, path, 125.0, "2026-06-12T13:01:00+02:00")

    with open(store.index_path, encoding="utf-8") as f:
        lines = [json.loads(l) for l in f if l.strip()]
    assert len(lines) == 1
    rec = lines[0]
    assert rec["uid"] == sample_meeting.uid
    assert rec["title"] == sample_meeting.title
    assert rec["platform"] == sample_meeting.platform.value
    assert rec["recorded_start"] == "2026-06-12T13:01:00+02:00"
    assert rec["duration_min"] == 2.1
    assert rec["note"] == os.path.basename(path)
    assert rec["quality"] == "live"


def test_index_mark_final_updates_only_matching(tmp_notes_dir, sample_meeting, make_meeting, fixed_now):
    import json

    store = NoteStore(tmp_notes_dir)
    p1 = store.create_note(sample_meeting)
    m2 = make_meeting(fixed_now + timedelta(hours=2), title="Jiný meeting")
    p2 = store.create_note(m2)
    store.index_add(sample_meeting, p1, 60.0, "x")
    store.index_add(m2, p2, 60.0, "y")

    store.index_mark_final(p1)

    with open(store.index_path, encoding="utf-8") as f:
        recs = {json.loads(l)["note"]: json.loads(l) for l in f if l.strip()}
    assert recs[os.path.basename(p1)]["quality"] == "final"
    assert recs[os.path.basename(p2)]["quality"] == "live"


def test_index_mark_final_no_index_file(tmp_notes_dir):
    store = NoteStore(tmp_notes_dir)
    store.index_mark_final(os.path.join(tmp_notes_dir, "neexistuje.md"))  # nesmí spadnout


def test_replace_transcript_with_speakers(tmp_notes_dir, sample_meeting):
    store = NoteStore(tmp_notes_dir)
    path = store.create_note(sample_meeting)
    store.replace_transcript(
        path,
        [
            (0.0, 2.0, "Dobrý den.", "Ivan"),
            (2.5, 4.0, "Dobrý den i vám.", "Ostatní"),
            (5.0, 6.0, "Bez mluvčího.", None),
            (7.0, 8.0, "Stará trojice."),
        ],
    )
    text = _read(path)
    assert "[00:00:00] Ivan: Dobrý den." in text
    assert "[00:00:02] Ostatní: Dobrý den i vám." in text
    assert "[00:00:05] Bez mluvčího." in text
    assert "[00:00:07] Stará trojice." in text
