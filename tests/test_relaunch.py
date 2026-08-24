"""Tests for sentry.relaunch (Cycle 1 Sentinel Core)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sentry.relaunch import (
    STANDARD_ENDPOINT_ENV,
    Relauncher,
    RelaunchResult,
)

START_1 = "========== CYCLE 1  21:02:33Z ==========\n"
DONE_1 = "========== CYCLE 1 done ==========\n"
START_2 = "========== CYCLE 2  21:03:00Z ==========\n"


def _make_project(tmp_path: Path, cycles_text: str) -> Path:
    (tmp_path / "cycles.out").write_text(cycles_text, encoding="utf-8")
    (tmp_path / "run-cycles.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    return tmp_path


def test_build_env_hardcodes_standard_config(tmp_path):
    rel = Relauncher(tmp_path)
    env = rel.build_env()
    assert env["FIVE_BASE_URL"] == "http://192.168.1.157:8080/v1"
    assert env["FIVE_MODEL"] == "fast-qwen"
    assert env["FIVE_LARGE_URL"] == "http://192.168.1.161:8081/v1"
    assert env["FIVE_LARGE_MODEL"] == "qwen"
    assert env["FIVE_MAX_TOKENS"] == "65536"


def test_build_env_never_inherits_session_endpoint_vars(tmp_path, monkeypatch):
    # A session FIVE_* var must NOT leak into the clean env.
    monkeypatch.setenv("FIVE_MODEL", "session-model-should-not-appear")
    monkeypatch.setenv("FIVE_BASE_URL", "http://session:9999/v1")
    rel = Relauncher(tmp_path)
    env = rel.build_env()
    assert env["FIVE_MODEL"] == "fast-qwen"
    assert env["FIVE_BASE_URL"] == "http://192.168.1.157:8080/v1"


def test_build_env_is_fresh_not_session_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_SESSION_VAR", "leak")
    rel = Relauncher(tmp_path)
    env = rel.build_env()
    assert "SOME_SESSION_VAR" not in env


def test_build_command(tmp_path):
    rel = Relauncher(tmp_path)
    assert rel.build_command(3) == ["bash", str(tmp_path / "run-cycles.sh"), "--start-cycle", "3"]


def test_relaunch_spawns_first_not_done_cycle(tmp_path):
    _make_project(tmp_path, START_1 + DONE_1 + START_2)
    rel = Relauncher(tmp_path)
    mock_proc = MagicMock()
    mock_proc.pid = 4242
    with patch.object(rel, "_spawn", return_value=mock_proc) as spawn:
        result = rel.relaunch()
    assert result.launched is True
    assert result.cycle == 2
    assert result.pid == 4242
    assert result.command == ["bash", str(tmp_path / "run-cycles.sh"), "--start-cycle", "2"]
    assert result.env["FIVE_MODEL"] == "fast-qwen"
    spawn.assert_called_once()
    args, _kwargs = spawn.call_args
    # _spawn(command, env, cwd) — env is the second positional arg
    assert args[1]["FIVE_BASE_URL"] == "http://192.168.1.157:8080/v1"


def test_relaunch_no_not_done_cycle(tmp_path):
    _make_project(tmp_path, START_1 + DONE_1)
    rel = Relauncher(tmp_path)
    with patch.object(rel, "_spawn") as spawn:
        result = rel.relaunch()
    assert result.launched is False
    assert result.reason == "no not-done cycle to relaunch"
    spawn.assert_not_called()


def test_relaunch_driver_missing(tmp_path):
    (tmp_path / "cycles.out").write_text(START_1, encoding="utf-8")
    # no run-cycles.sh
    rel = Relauncher(tmp_path)
    with patch.object(rel, "_spawn") as spawn:
        result = rel.relaunch()
    assert result.launched is False
    assert "driver not found" in result.reason
    spawn.assert_not_called()


def test_relaunch_result_to_dict(tmp_path):
    r = RelaunchResult(True, cycle=1, pid=7, command=["bash", "x"], env={"A": "1"}, reason="ok")
    d = r.to_dict()
    assert d["launched"] is True
    assert d["cycle"] == 1
    assert d["pid"] == 7
    assert d["command"] == ["bash", "x"]
    assert d["env"] == {"A": "1"}


def test_standard_endpoint_env_is_verbatim():
    assert STANDARD_ENDPOINT_ENV == {
        "FIVE_BASE_URL": "http://192.168.1.157:8080/v1",
        "FIVE_MODEL": "fast-qwen",
        "FIVE_LARGE_URL": "http://192.168.1.161:8081/v1",
        "FIVE_LARGE_MODEL": "qwen",
        "FIVE_MAX_TOKENS": "65536",
    }
