"""Testy centralizovaného uložení modelů (app/model_store.py, W2).

``faster_whisper`` je v testech MagicMock (conftest); ``download_model`` proto
podstrkujeme jako fake, který vytvoří reálné soubory v ``output_dir``. Velikostní
práh ``model.bin`` snižujeme přes monkeypatch, ať testy nepíšou desítky MB.
``_isolate_cwd`` (conftest) přepne cwd do tmp_path, takže ``models/`` je tam.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import faster_whisper  # MagicMock z conftestu
import pytest

from app import model_store


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    """Sniž práh velikosti model.bin (ať se nepíšou desítky MB) a po každém testu
    OBNOV ``faster_whisper.download_model`` — testy ho přepisují přímou přiřazením
    do sdíleného mocku, takže bez obnovy by „raising" fake protekl do dalších
    testů (a tam by ``ensure_model`` retry uspával 30 s → vypadalo to jako hang)."""
    monkeypatch.setattr(model_store, "_MIN_MODEL_BIN_BYTES", 10)
    orig = getattr(faster_whisper, "download_model", None)
    yield
    faster_whisper.download_model = orig


def _make_model_files(d: str, mb: int = 50) -> None:
    """Vytvoří kompletní sadu souborů modelu (model.bin + podpůrné)."""
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "model.bin"), "wb") as f:
        f.write(b"\0" * mb)
    for fn in ("config.json", "tokenizer.json", "vocabulary.txt"):
        with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
            f.write("{}")


# --------------------------------------------------------------- is_ready

def test_is_ready_kompletni_vs_neuplny():
    assert model_store.is_ready("small") is False  # nic
    d = model_store.model_dir("small")
    _make_model_files(d, mb=50)
    assert model_store.is_ready("small") is True
    # model.bin pod prahem -> není ready
    with open(os.path.join(d, "model.bin"), "wb") as f:
        f.write(b"\0" * 5)
    assert model_store.is_ready("small") is False
    # chybí podpůrný soubor -> není ready
    _make_model_files(d, mb=50)
    os.remove(os.path.join(d, "config.json"))
    assert model_store.is_ready("small") is False


def test_is_ready_vocabulary_txt_nebo_json():
    d = model_store.model_dir("large-v3-turbo")
    _make_model_files(d, mb=50)
    os.remove(os.path.join(d, "vocabulary.txt"))
    assert model_store.is_ready("large-v3-turbo") is False
    with open(os.path.join(d, "vocabulary.json"), "w", encoding="utf-8") as f:
        f.write("{}")
    assert model_store.is_ready("large-v3-turbo") is True  # .json stačí


# --------------------------------------------------------------- migrace

def test_migrace_ze_stare_cache_bez_stahovani():
    faster_whisper.download_model = _fail_if_called
    snap = os.path.join(
        "models", "models--Systran--faster-whisper-small", "snapshots", "rev1"
    )
    _make_model_files(snap, mb=50)
    assert model_store.is_ready("small") is False  # nový layout zatím prázdný

    assert model_store.ensure_model("small") is True  # migruje, NEstahuje
    assert model_store.is_ready("small") is True
    # stará cache zůstává (žádné mazání = žádná ztráta jediné kopie)
    assert os.path.isdir(os.path.join("models", "models--Systran--faster-whisper-small"))


# --------------------------------------------------------------- stažení

def test_ensure_stahne_kdyz_chybi():
    calls = []

    def fake(name, output_dir=None):
        calls.append((name, output_dir))
        _make_model_files(output_dir, mb=50)

    faster_whisper.download_model = fake
    assert model_store.ensure_model("small") is True
    assert calls and calls[0][0] == "small"
    assert model_store.is_ready("small") is True


def test_ensure_ready_se_nestahuje():
    _make_model_files(model_store.model_dir("small"), mb=50)
    faster_whisper.download_model = _fail_if_called
    assert model_store.ensure_model("small") is True  # už hotovo -> nesahá na síť


# ---------------------------------------------------- pending / aktualizace

def test_apply_pending_swapne_novou_verzi():
    d = model_store.model_dir("small")
    _make_model_files(d, mb=50)             # aktuální
    _make_model_files(d + ".update", mb=80)  # stažená novější

    n = model_store.apply_pending_updates(("small",))

    assert n == 1
    assert not os.path.isdir(d + ".update")          # pending nasazen a uklizen
    assert os.path.getsize(os.path.join(d, "model.bin")) == 80  # nová verze


def test_check_for_updates_ready_a_current_a_offline():
    d = model_store.model_dir("small")
    _make_model_files(d, mb=50)

    # novější (jiná velikost) -> ready (pending vznikne)
    faster_whisper.download_model = lambda name, output_dir=None: _make_model_files(output_dir, mb=80)
    assert model_store.check_for_updates("small") == "ready"
    assert os.path.isdir(d + ".update")

    # shodná velikost -> current (pending se zahodí)
    faster_whisper.download_model = lambda name, output_dir=None: _make_model_files(output_dir, mb=50)
    assert model_store.check_for_updates("small") == "current"
    assert not os.path.isdir(d + ".update")

    # síť mimo -> offline (a nic se nerozbije)
    def _raise(name, output_dir=None):
        raise RuntimeError("offline")

    faster_whisper.download_model = _raise
    assert model_store.check_for_updates("small") == "offline"
    assert model_store.is_ready("small") is True  # původní model nedotčen


def _fail_if_called(*a, **k):
    raise AssertionError("download_model nemělo být voláno")


def _cfg(live="small", post="large-v3-turbo"):
    return SimpleNamespace(live_model=live, post_model=post)
