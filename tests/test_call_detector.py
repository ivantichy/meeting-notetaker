"""Testy čisté logiky detekce hovoru (detect_label)."""
from app.call_detector import detect_label


def test_no_entries():
    assert detect_label([]) is None


def test_nothing_active():
    assert detect_label([("np:C:#apps#ms-teams.exe", 133999), ("np:C:#x#chrome.exe", 5)]) is None


def test_teams_nonpackaged_active():
    assert detect_label([("np:C:#Program Files#Teams#ms-teams.exe", 0)]) == "Teams hovor"


def test_teams_packaged_active():
    assert detect_label([("MSTeams_8wekyb3d8bbwe", 0)]) == "Teams hovor"


def test_chrome_active():
    assert detect_label([("np:C:#chrome#chrome.exe", 0)]) == "Hovor v Chrome (Meet)"


def test_teams_priority_over_browser():
    entries = [
        ("np:C:#chrome#chrome.exe", 0),
        ("np:C:#teams#ms-teams.exe", 0),
    ]
    assert detect_label(entries) == "Teams hovor"


def test_unwatched_app_ignored():
    assert detect_label([("np:C:#apps#zoom.exe", 0)]) is None


def test_case_insensitive():
    assert detect_label([("np:C:#X#Ms-Teams.EXE", 0)]) == "Teams hovor"
