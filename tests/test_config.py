"""Testy konfigurace: round-trip, poškozený soubor, neznámé klíče (M10)."""
import json
import os

from app.config import AppConfig, load_config, save_config


def test_round_trip_preserves_all_fields(tmp_path):
    path = str(tmp_path / "config.json")
    cfg = AppConfig(
        ics_url="https://example.com/secret-token.ics",
        language="cs",
        live_model="small",
        post_model="large-v3-turbo",
        poll_minutes=7,
        early_stop_grace_s=42,
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded == cfg
    assert loaded.ics_url == "https://example.com/secret-token.ics"
    assert loaded.poll_minutes == 7
    assert loaded.early_stop_grace_s == 42


def test_missing_file_returns_defaults(tmp_path):
    path = str(tmp_path / "neexistuje.json")
    assert load_config(path) == AppConfig()


def test_unknown_keys_ignored(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"ics_url": "https://x/y.ics", "language": "en", "neznamy_klic": 123},
            f,
        )
    cfg = load_config(path)
    assert cfg.ics_url == "https://x/y.ics"
    assert cfg.language == "en"
    assert not hasattr(cfg, "neznamy_klic")


def test_corrupt_json_backed_up_not_silently_lost(tmp_path):
    """Poškozený config se nesmí tiše ztratit — zazálohuje se do .corrupt."""
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"ics_url": "https://secret/token.ics", BROKEN')
    cfg = load_config(path)
    # vrátí defaulty (prázdná ics_url)
    assert cfg == AppConfig()
    # ale původní (poškozený) obsah je zachován v záloze
    backup = path + ".corrupt"
    assert os.path.exists(backup)
    with open(backup, encoding="utf-8") as f:
        assert "https://secret/token.ics" in f.read()


def test_non_object_json_backed_up(tmp_path):
    path = str(tmp_path / "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump([1, 2, 3], f)
    cfg = load_config(path)
    assert cfg == AppConfig()
    assert os.path.exists(path + ".corrupt")


def test_save_is_atomic_no_tmp_left(tmp_path):
    path = str(tmp_path / "config.json")
    save_config(AppConfig(ics_url="https://x/y.ics"), path)
    leftovers = [n for n in os.listdir(tmp_path) if ".tmp." in n]
    assert leftovers == []


def test_example_matches_code_defaults():
    """config.example.json musí odpovídat výchozím hodnotám v kódu (M10)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "config.example.json"), encoding="utf-8") as f:
        example = json.load(f)
    defaults = AppConfig()
    # ics_url je v příkladu záměrně prázdná
    assert example["language"] == defaults.language
    assert example["early_stop_grace_s"] == defaults.early_stop_grace_s
    assert example["live_model"] == defaults.live_model
    assert example["post_model"] == defaults.post_model
    assert example["arm_window_s"] == defaults.arm_window_s
    assert example["stop_grace_s"] == defaults.stop_grace_s
    assert example["no_call_timeout_s"] == defaults.no_call_timeout_s
