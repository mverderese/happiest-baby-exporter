"""Exporter helpers shared with happiest-baby-actions.py."""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("click")

SCRIPT = Path(__file__).resolve().parent.parent / "happiest-baby-exporter.py"
spec = importlib.util.spec_from_file_location("exporter", SCRIPT)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def piped_stdin(monkeypatch, text):
    read_end, write_end = os.pipe()
    os.write(write_end, text.encode())
    os.close(write_end)
    monkeypatch.setattr(sys, "stdin", os.fdopen(read_end))


def test_credentials_can_be_piped(monkeypatch, capsys):
    piped_stdin(monkeypatch, "me@example.com\n")
    monkeypatch.setattr(exporter, "authenticate", lambda email, password: {"IdToken": f"{email}:{password}"})
    assert exporter.get_id_token(None, "pw", None, None) == "me@example.com:pw"
    assert capsys.readouterr().out == ""  # progress and prompts go to stderr


def test_empty_stdin_fails_with_a_clear_error(monkeypatch, capsys):
    piped_stdin(monkeypatch, "")
    with pytest.raises(SystemExit) as exited:
        exporter.get_id_token(None, None, None, None)
    assert exited.value.code == 1
    assert "No credentials provided" in capsys.readouterr().err


def test_sessions_that_ended_before_the_window_are_dropped():
    def level(session, start, seconds):
        return {"sessionId": session, "startTimeFormatted": start, "stateDuration": seconds}

    levels = [
        level("before", "2026-10-12T22:00:00-0400", 3600),  # ended 23:00, before the cutoff
        level("spans", "2026-10-13T04:25:36-0400", 5664),   # began before the cutoff, ended after
        level("spans", "2026-10-13T06:00:00-0400", 5496),
    ]
    cutoff = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)  # 05:00 EDT
    kept = exporter.drop_sessions_ended_before(levels, cutoff)
    assert [lv["sessionId"] for lv in kept] == ["spans", "spans"]
