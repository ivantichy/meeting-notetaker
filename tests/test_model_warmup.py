"""Testy předstažení modelů na pozadí (app/model_warmup.py, W1).

``faster_whisper`` je v testech MagicMock (viz conftest), takže ``download_model``
nic reálně nestahuje — ověřujeme jen ROZHODOVÁNÍ (co/kam/v jakém pořadí se stahuje,
že chyba nepropadne) a POZOROVATELNÝ STAV (``downloading`` → ``finished``), který
UI používá k tomu, aby nezkoušelo nahrávat, dokud model není stažený.
``model_is_downloaded`` čte relativní ``models/`` v cwd; conftest každý test
izoluje do ``tmp_path``, takže cache je defaultně prázdná.
"""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import faster_whisper  # MagicMock z conftestu

from app import model_warmup


def _cfg(live: str = "small", post: str = "large-v3-turbo") -> SimpleNamespace:
    """Minimální config jen s poli, která warmup čte."""
    return SimpleNamespace(live_model=live, post_model=post)


def _fresh_download_mock() -> Mock:
    """Čerstvý ``download_model`` mock (modul-mock je sdílený, mezi testy ho měníme)."""
    m = Mock()
    faster_whisper.download_model = m
    return m


def _mark_downloaded(org: str, name: str) -> None:
    """Vyrobí v ``models/`` neprázdný snapshot, ať ``model_is_downloaded`` vrátí True.

    Cesty jsou relativní ke cwd (== tmp_path díky conftest fixture)."""
    snap = os.path.join("models", f"models--{org}--{name}", "snapshots", "rev1")
    os.makedirs(snap, exist_ok=True)
    with open(os.path.join(snap, "model.bin"), "w", encoding="utf-8") as f:
        f.write("x")


def test_stahne_oba_chybejici_modely():
    dl = _fresh_download_mock()

    wu = model_warmup.start_model_warmup(_cfg("small", "large-v3-turbo"))
    wu.join(timeout=5)

    assert not wu.is_alive()
    assert wu.finished
    # Oba modely, ve správném pořadí (živý nejdřív).
    assert [c.args[0] for c in dl.call_args_list] == ["small", "large-v3-turbo"]
    # Vždy do TÉŽE cache, ze které pak nahrávání model čte.
    assert all(
        c.kwargs.get("cache_dir") == model_warmup.DOWNLOAD_ROOT
        for c in dl.call_args_list
    )


def test_preskoci_uz_stazeny():
    _mark_downloaded("Systran", "faster-whisper-small")
    dl = _fresh_download_mock()

    wu = model_warmup.start_model_warmup(_cfg("small", "small"))
    wu.join(timeout=5)

    dl.assert_not_called()
    assert wu.finished


def test_deduplikuje_shodny_live_a_post():
    dl = _fresh_download_mock()

    wu = model_warmup.start_model_warmup(_cfg("small", "small"))
    wu.join(timeout=5)

    assert [c.args[0] for c in dl.call_args_list] == ["small"]


def test_chyba_jednoho_nezastavi_druhy_ani_neshodi():
    dl = _fresh_download_mock()
    dl.side_effect = [RuntimeError("offline"), None]

    wu = model_warmup.start_model_warmup(_cfg("small", "large-v3-turbo"))
    wu.join(timeout=5)

    # První spadl, druhý se přesto zkusil; vlákno čistě doběhlo (chyba nepropadla).
    assert [c.args[0] for c in dl.call_args_list] == ["small", "large-v3-turbo"]
    assert not wu.is_alive()
    assert wu.finished


def test_prazdny_config_nestahuje_nic():
    dl = _fresh_download_mock()

    wu = model_warmup.start_model_warmup(_cfg("", ""))
    wu.join(timeout=5)

    dl.assert_not_called()
    assert wu.finished


def test_stav_downloading_pak_finished():
    """Během stahování ``downloading`` hlásí jméno modelu; po doběhnutí je None
    a ``finished`` True — přesně to, co UI čte pro indikátor a gating."""
    started = threading.Event()
    release = threading.Event()

    def fake_download(name, cache_dir=None):  # noqa: ANN001
        started.set()
        release.wait(2)

    faster_whisper.download_model = fake_download

    wu = model_warmup.start_model_warmup(_cfg("small", "small"))  # dedup -> 1×
    assert started.wait(2)            # stahování se rozběhlo
    assert wu.downloading == "small"  # UI vidí, který model se táhne
    assert not wu.finished

    release.set()
    wu.join(timeout=5)

    assert wu.downloading is None
    assert wu.finished
    assert not wu.is_alive()


def test_materialize_symlinks_prevadi_na_realne_soubory(tmp_path):
    """Symlink snapshots/*/model.bin -> blobs/* se převede na reálný soubor.

    Důvod: zabalený CTranslate2 symlink neotevře. Na Windows bez práv nejde
    symlink vytvořit -> test se přeskočí (CI běží na Linuxu, kde to projde)."""
    import pytest

    base = tmp_path / "models" / "models--Systran--faster-whisper-small"
    (base / "blobs").mkdir(parents=True)
    blob = base / "blobs" / "deadbeef"
    blob.write_bytes(b"WHISPER-MODEL-BYTES")
    snap = base / "snapshots" / "rev1"
    snap.mkdir(parents=True)
    link = snap / "model.bin"
    try:
        os.symlink(os.path.join("..", "..", "blobs", "deadbeef"), link)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinky nelze vytvořit v tomto prostředí: {exc}")
    assert os.path.islink(link)

    # cwd == tmp_path (conftest _isolate_cwd), takže relativní "models" je tady.
    converted = model_warmup.materialize_symlinks("models")

    assert converted == 1
    assert not os.path.islink(link)
    assert link.read_bytes() == b"WHISPER-MODEL-BYTES"
    # Idempotence: druhý běh už nic nepřevádí (je to reálný soubor).
    assert model_warmup.materialize_symlinks("models") == 0
