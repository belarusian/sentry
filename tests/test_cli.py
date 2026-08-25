"""Tests for sentry.cli (Cycle 6 CLI + release).

Uses ``patch.object(instance, 'method')`` throughout — never constructor-level
patches. Writable paths come from the ``tmp_path`` fixture; the watched project
dir is never written to by ``check``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from sentry.cli import EXIT_ACTION, EXIT_OK, EXIT_USAGE, SentryCLI, main
from sentry.relaunch import RelaunchResult
from sentry.sentinel import DetectionResult
from sentry.sentrylog import SentryLog
from sentry.stall import StallDecision, StallResult

START_1 = "========== CYCLE 1  21:02:33Z =========="
DONE_1 = "========== CYCLE 1 done =========="
START_2 = "========== CYCLE 2  21:03:00Z =========="


def _make_project(tmp_path: Path, cycles_text: str) -> Path:
    (tmp_path / "cycles.out").write_text(cycles_text, encoding="utf-8")
    (tmp_path / "run-cycles.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    return tmp_path


def _mock_monitor(stalled: bool = False, socket_live: bool = False, growing: bool = False,
                  inner_pid: int | None = None) -> MagicMock:
    m = MagicMock()
    m.detect_stall.return_value = StallResult(stalled=stalled, reason="x")
    m.any_socket_live.return_value = socket_live
    m.trajectory_growing.return_value = growing
    m.find_inner_pid.return_value = inner_pid
    return m


# ---------------------------------------------------------------------------
# check (READ-ONLY)
# ---------------------------------------------------------------------------

def test_check_healthy_exit_0(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=True), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="no cycle in flight")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_check()
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "verdict: HEALTHY" in out
    assert "driver: alive" in out


def test_check_driver_death_exit_1(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n" + START_2 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=False), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(True, cycle=2, reason="driver death")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_check()
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "driver-death: DETECTED cycle 2" in out
    assert "verdict: ACTION NEEDED" in out


def test_check_wall_kill_exit_1(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n" + START_2 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=False), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(True, cycle=2, reason="wall-kill")):
        code = cli.run_check()
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "wall-kill-no-merge: DETECTED cycle 2" in out


def test_check_stall_kill_exit_1(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=True, socket_live=False, growing=False, inner_pid=1234)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=True), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_check()
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "stall: kill" in out


def test_check_stall_wait_exit_0(tmp_path, capsys):
    """WAIT requires the corroborating movement signal (trajectory growing),
    not a bare ESTAB socket (TICKET-022)."""
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=True, socket_live=True, growing=True)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=True), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_check()
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "stall: wait" in out


def test_check_stall_bare_estab_kills_exit_1(tmp_path, capsys):
    """TICKET-022: a hung-but-ESTAB socket with no progress must KILL, not WAIT."""
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=True, socket_live=True, growing=False, inner_pid=1234)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=True), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_check()
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "stall: kill" in out


def test_check_writes_nothing_under_watched_project(tmp_path):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    before = sorted(p.name for p in tmp_path.iterdir())
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "is_driver_process_alive", return_value=True), \
         patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")), \
         patch.object(cli.sentinel, "detect_wall_kill_no_merge",
                      return_value=DetectionResult(False, reason="none")):
        cli.run_check()
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


# ---------------------------------------------------------------------------
# rescue
# ---------------------------------------------------------------------------

def test_rescue_dry_run_driver_death_exit_1_no_writes(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n" + START_2 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(True, cycle=2, reason="driver death")):
        code = cli.run_rescue(dry_run=True)
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "[dry-run] would relaunch from cycle 2" in out
    assert "dry-run: no writes" in out
    # No SentryLog file should have been created under the watched project.
    assert not (tmp_path / "ai" / "sentry-stall.log").exists()


def test_rescue_dry_run_healthy_exit_0(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_rescue(dry_run=True)
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "relaunch: none" in out


def test_rescue_apply_driver_death_relays_and_logs(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n" + START_2 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    cli.monitor.log = SentryLog(tmp_path / "rescue.log")
    result = RelaunchResult(launched=True, cycle=2, pid=99,
                            command=["bash", str(tmp_path / "run-cycles.sh"),
                                     "--start-cycle", "2"],
                            reason="relaunched driver from cycle 2")
    with patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(True, cycle=2, reason="driver death")), \
         patch.object(cli.relauncher, "relaunch", return_value=result):
        code = cli.run_rescue(dry_run=False)
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "relaunch: relaunched from cycle 2 (pid 99)" in out
    # A decision was appended to the SentryLog.
    assert cli.monitor.log.size() >= 1
    events = [e.event_type for e in cli.monitor.log.read_all()]
    assert "rescue.relaunch" in events


def test_rescue_apply_healthy_exit_0(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=False)
    with patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_rescue(dry_run=False)
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "relaunch: none" in out


def test_rescue_apply_stall_kill_exit_1(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    cli = SentryCLI(tmp_path)
    cli.monitor = _mock_monitor(stalled=True)
    cli.monitor.handle_stall.return_value = StallDecision(action="kill", pid=1234,
                                                          reason="stalled", logged=True)
    with patch.object(cli.sentinel, "detect_driver_death",
                      return_value=DetectionResult(False, reason="none")):
        code = cli.run_rescue(dry_run=False)
    out = capsys.readouterr().out
    assert code == EXIT_ACTION
    assert "stall: kill" in out


# ---------------------------------------------------------------------------
# argparse / entry point
# ---------------------------------------------------------------------------

def test_main_no_command_usage_error():
    assert main([]) == EXIT_USAGE


def test_main_check_missing_dir_usage_error():
    assert main(["check"]) == EXIT_USAGE


def test_main_help_exit_0(capsys):
    assert main(["--help"]) == EXIT_OK
    assert "sentry" in capsys.readouterr().out


def test_main_check_dispatch(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    with patch.object(SentryCLI, "run_check", return_value=EXIT_OK) as rc:
        code = main(["check", str(tmp_path)])
    assert code == EXIT_OK
    rc.assert_called_once()


def test_main_rescue_dispatch_dry_run(tmp_path, capsys):
    _make_project(tmp_path, START_1 + "\n" + DONE_1 + "\n")
    with patch.object(SentryCLI, "run_rescue", return_value=EXIT_OK) as rr:
        code = main(["rescue", str(tmp_path), "--dry-run"])
    assert code == EXIT_OK
    rr.assert_called_once_with(dry_run=True)
