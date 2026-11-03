"""Production CLI does not promote episodic memories without explicit consent."""
import sys

import pytest

from src.episodic_memory.__main__ import main


def test_promote_cli_requires_confirmation_before_source_or_vault_access(tmp_path,monkeypatch):
    state=tmp_path/"state"
    monkeypatch.setattr(sys,"argv",[
        "episodic_memory","--owner","alice","--state-dir",str(state),
        "promote","--candidate-id","a"*32,
        "--vault-root",str(tmp_path/"mabel-vault"),
        "--protected-root",str(tmp_path/"core-of-truth"),
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code==2
    assert list(tmp_path.rglob("*.md"))==[]


def test_promote_cli_requires_explicit_pilot_even_when_confirmed(tmp_path,monkeypatch):
    monkeypatch.delenv("MABEL_EPISODIC_CAPTURE_ENABLED",raising=False)
    monkeypatch.setattr(sys,"argv",[
        "episodic_memory","--owner","alice","--state-dir",str(tmp_path/"state"),
        "promote","--candidate-id","b"*32,
        "--vault-root",str(tmp_path/"mabel-vault"),
        "--protected-root",str(tmp_path/"core-of-truth"),
        "--confirm-promote",
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code==2
    assert list(tmp_path.rglob("*.md"))==[]


def test_promote_cli_is_discoverable_and_requires_separate_vault_roots(tmp_path,monkeypatch):
    monkeypatch.setattr(sys,"argv",[
        "episodic_memory","--owner","alice","--state-dir",str(tmp_path/"state"),
        "promote","--candidate-id","c"*32,"--confirm-promote",
    ])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code==2
