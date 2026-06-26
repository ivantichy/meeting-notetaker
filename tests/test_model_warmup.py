"""Testy běhového obalu předstažení (app/model_warmup.py, W1).

Veškerá logika je teď v ``app.model_store``; warm-up je jen daemon, který volá
``model_store`` a zveřejňuje stav (downloading/finished/failed) pro UI. Proto tu
``model_store`` mockujeme a ověřujeme JEN orchestraci a stav.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from app import model_store, model_warmup


def _cfg(live: str = "small", post: str = "large-v3-turbo") -> SimpleNamespace:
    return SimpleNamespace(live_model=live, post_model=post)


def _stub_store(monkeypatch, *, ready=lambda n: False, ensure=lambda n: True):
    monkeypatch.setattr(model_store, "apply_pending_updates", lambda names: 0)
    monkeypatch.setattr(model_store, "is_ready", ready)
    monkeypatch.setattr(model_store, "ensure_model", ensure)


def test_zajisti_oba_chybejici_modely(monkeypatch):
    calls = []
    _stub_store(monkeypatch, ensure=lambda n: calls.append(n) or True)

    wu = model_warmup.start_model_warmup(_cfg("small", "large-v3-turbo"))
    wu.join(timeout=5)

    assert calls == ["small", "large-v3-turbo"]  # živý nejdřív
    assert wu.finished and not wu.failed
    assert not wu.is_alive()


def test_preskoci_ready_modely(monkeypatch):
    calls = []
    _stub_store(monkeypatch, ready=lambda n: True, ensure=lambda n: calls.append(n) or True)

    wu = model_warmup.start_model_warmup(_cfg("small", "small"))
    wu.join(timeout=5)

    assert calls == []  # ready -> ensure se nevolá
    assert wu.finished


def test_deduplikuje_shodny_live_a_post(monkeypatch):
    calls = []
    _stub_store(monkeypatch, ensure=lambda n: calls.append(n) or True)

    wu = model_warmup.start_model_warmup(_cfg("small", "small"))
    wu.join(timeout=5)

    assert calls == ["small"]


def test_neuspech_se_oznaci_a_nezastavi_druhy(monkeypatch):
    _stub_store(monkeypatch, ensure=lambda n: n != "large-v3-turbo")

    wu = model_warmup.start_model_warmup(_cfg("small", "large-v3-turbo"))
    wu.join(timeout=5)

    assert "large-v3-turbo" in wu.failed
    assert "small" not in wu.failed
    assert wu.finished and not wu.is_alive()


def test_vyjimka_v_ensure_je_failed_a_nepropadne(monkeypatch):
    def boom(n):
        raise RuntimeError("offline")

    _stub_store(monkeypatch, ensure=boom)

    wu = model_warmup.start_model_warmup(_cfg("small", ""))
    wu.join(timeout=5)

    assert "small" in wu.failed
    assert wu.finished  # vlákno čistě doběhlo i přes výjimku


def test_stav_downloading_pak_finished(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow_ensure(n):
        started.set()
        release.wait(2)
        return True

    _stub_store(monkeypatch, ensure=slow_ensure)

    wu = model_warmup.start_model_warmup(_cfg("small", "small"))  # dedup -> 1×
    assert started.wait(2)
    assert wu.downloading == "small"
    assert not wu.finished

    release.set()
    wu.join(timeout=5)

    assert wu.downloading is None
    assert wu.finished
    assert not wu.is_alive()
