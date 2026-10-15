"""The exporter CLI on top of the shared happiest_baby package."""

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


def test_sleep_csv_rows_come_from_the_shared_client():
    levels = [
        {"sessionId": "s1", "type": "asleep", "startTime": "2026-10-13 04:25:36.000",
         "startTimeFormatted": "2026-10-13T04:25:36-0400", "stateDuration": 600},
        {"sessionId": "s1", "type": "soothing", "startTime": "2026-10-13 04:35:36.000",
         "startTimeFormatted": "2026-10-13T04:35:36-0400", "stateDuration": 60},
        {"sessionId": "m1", "type": "manual", "startTime": "2026-10-13 14:50:00.000",
         "startTimeFormatted": "2026-10-13T14:50:00-0400", "stateDuration": 2400},
    ]
    detailed = [{"sessionId": "m1", "level": "MANUAL", "label": "in_arms"}]

    class Client:
        def sleep_levels(self, baby_id, from_date, to_date, tz):
            return levels, detailed

    window = datetime(2026, 10, 13, tzinfo=timezone.utc)
    raw, rows = exporter.fetch_sleep_sessions(Client(), "b", window, window, "America/New_York")
    assert raw == levels
    assert rows == [
        {"sessionId": "s1", "startTime": "2026-10-13 04:25:36", "startTimeFormatted": "2026-10-13T04:25:36-0400",
         "stateDuration": 660, "type": "asleep,soothing", "source": "snoo", "label": "", "note": ""},
        {"sessionId": "m1", "startTime": "2026-10-13 14:50:00", "startTimeFormatted": "2026-10-13T14:50:00-0400",
         "stateDuration": 2400, "type": "manual", "source": "manual", "label": "in_arms", "note": ""},
    ]
