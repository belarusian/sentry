"""Tests for sentry.stall.StallMonitor (Cycle 4 stall handling).

Uses ``patch.object(instance, 'method')`` throughout — never constructor-level
patches. Writable paths come from the ``tmp_path`` fixture; the watched project
dir is never written to.
"""

from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import patch

import pytest

from sentry.sentrylog import SentryLog
from sentry.stall import StallDecision, StallMonitor, StallResult

# A fixed reference timestamp for deterministic boundary tests.
BASE = 1_000_000.0


def _make_monitor(tmp_path: Path, **kwargs) -> StallMonitor:
    """Build a StallMonitor whose log lives under tmp_path (writable)."""
    log = SentryLog(tmp_path / "stall.log")
    return StallMonitor(
        tmp_path / "watched",
        log=log,
        trajectories_dir=tmp_path / "trajectories",
        git_dir=tmp_path / "proj",
        **kwargs,
    )


def _patch_movement(monitor: StallMonitor, base: float):
    """Patch all four movement signals to return a fixed ``base`` timestamp."""
    return (
        patch.object(monitor, "_git_commit_time", return_value=base),
        patch.object(monitor, "_branch_ref_time", return_value=base),
        patch.object(monitor, "_cycles_out_mtime", return_value=base),
        patch.object(monitor, "_newest_trajectory_mtime", return_value=base),
    )


# -- detect_stall: 60-minute boundary (59 vs 61 min) ------------------------


def test_detect_stall_not_stalled_at_59_min(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    p1, p2, p3, p4 = _patch_movement(monitor, BASE)
    with p1, p2, p3, p4:
        result = monitor.detect_stall(now=BASE + 59 * 60)
    assert isinstance(result, StallResult)
    assert result.stalled is False
    assert result.last_movement == BASE
    assert result.age_seconds == pytest.approx(59 * 60)


def test_detect_stall_stalled_at_61_min(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    p1, p2, p3, p4 = _patch_movement(monitor, BASE)
    with p1, p2, p3, p4:
        result = monitor.detect_stall(now=BASE + 61 * 60)
    assert result.stalled is True
    assert result.age_seconds == pytest.approx(61 * 60)


def test_detect_stall_uses_max_movement_signal(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    # Newest signal is the trajectory mtime (BASE + 1000).
    with (
        patch.object(monitor, "_git_commit_time", return_value=BASE),
        patch.object(monitor, "_branch_ref_time", return_value=None),
        patch.object(monitor, "_cycles_out_mtime", return_value=BASE + 500),
        patch.object(monitor, "_newest_trajectory_mtime", return_value=BASE + 1000),
    ):
        result = monitor.detect_stall(now=BASE + 1000 + 61 * 60)
    assert result.last_movement == BASE + 1000
    assert result.stalled is True


def test_detect_stall_no_movement_signal(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_git_commit_time", return_value=None),
        patch.object(monitor, "_branch_ref_time", return_value=None),
        patch.object(monitor, "_cycles_out_mtime", return_value=None),
        patch.object(monitor, "_newest_trajectory_mtime", return_value=None),
    ):
        result = monitor.detect_stall(now=BASE + 10 * 3600)
    assert result.stalled is False
    assert result.last_movement is None
    assert result.reason == "no movement signal"


# -- probe_sockets: live / dead / local-LISTEN ignored ----------------------


def test_probe_sockets_live_estab_to_remote_endpoint(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    ss_output = (
        "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
        "ESTAB  0      0      172.20.190.185:60230  192.168.1.157:8080 "
        'users:(("python3",pid=457311,fd=3))\n'
    )
    with patch.object(monitor, "_run_ss", return_value=ss_output):
        result = monitor.probe_sockets()
        # any_socket_live() re-runs the probe; it MUST run while the mock
        # is active, otherwise the real `ss -tnp` runs and the result
        # depends on ambient machine state (TICKET-016).
        assert monitor.any_socket_live() is True
    assert result["192.168.1.157:8080"] is True
    assert result["192.168.1.161:8081"] is False


def test_probe_sockets_ignores_local_listen(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    # A local LISTEN on 0.0.0.0:8080 must NOT count as a live endpoint.
    ss_output = (
        "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
        "LISTEN 0      5      0.0.0.0:8080           0.0.0.0:* \n"
    )
    with patch.object(monitor, "_run_ss", return_value=ss_output):
        result = monitor.probe_sockets()
        assert result["192.168.1.157:8080"] is False
        assert monitor.any_socket_live() is False


def test_probe_sockets_ignores_endpoint_in_local_column(tmp_path: Path) -> None:
    """TICKET-023: a host:port token in the *local* address column (not the
    peer/destination column) must NOT count as a live endpoint."""
    monitor = _make_monitor(tmp_path)
    # Local bind is the endpoint token; the peer is an unrelated host.
    ss_output = (
        "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
        "ESTAB  0      0      192.168.1.157:8080  10.0.0.5:443 "
        'users:(("python3",pid=457311,fd=3))\n'
    )
    with patch.object(monitor, "_run_ss", return_value=ss_output):
        result = monitor.probe_sockets()
        assert result["192.168.1.157:8080"] is False
        assert monitor.any_socket_live() is False


def test_probe_sockets_ignores_endpoint_in_process_path(tmp_path: Path) -> None:
    """TICKET-023: a host:port token in the process/fd column must NOT count
    as a live endpoint when the peer column is a different host."""
    monitor = _make_monitor(tmp_path)
    ss_output = (
        "State  Recv-Q Send-Q Local Address:Port  Peer Address:Port\n"
        "ESTAB  0      0      172.20.190.185:60230  10.0.0.5:443 "
        'users:(("python3",pid=457311,fd=3)) 192.168.1.157:8080\n'
    )
    with patch.object(monitor, "_run_ss", return_value=ss_output):
        result = monitor.probe_sockets()
        assert result["192.168.1.157:8080"] is False
        assert monitor.any_socket_live() is False


def test_probe_sockets_dead_when_no_output(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with patch.object(monitor, "_run_ss", return_value=""):
        result = monitor.probe_sockets()
        assert all(value is False for value in result.values())
        assert monitor.any_socket_live() is False


# -- trajectory_growing: two-sample size/mtime ------------------------------


def test_trajectory_growing_true_when_size_grows(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    traj = tmp_path / "trajectories" / "trajectory_0001.json"
    traj.parent.mkdir(parents=True, exist_ok=True)
    traj.write_text("{}")
    sizes = iter([100, 200])
    with (
        patch.object(monitor, "_newest_trajectory_path", return_value=traj),
        patch.object(monitor, "_sample_size", side_effect=lambda p: next(sizes)),
        patch.object(monitor, "_stat_mtime", return_value=BASE),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        assert monitor.trajectory_growing() is True


def test_trajectory_growing_false_when_flat(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    traj = tmp_path / "trajectories" / "trajectory_0001.json"
    traj.parent.mkdir(parents=True, exist_ok=True)
    traj.write_text("{}")
    with (
        patch.object(monitor, "_newest_trajectory_path", return_value=traj),
        patch.object(monitor, "_sample_size", return_value=100),
        patch.object(monitor, "_stat_mtime", return_value=BASE),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        assert monitor.trajectory_growing() is False


def test_trajectory_growing_true_when_mtime_advances(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    traj = tmp_path / "trajectories" / "trajectory_0001.json"
    traj.parent.mkdir(parents=True, exist_ok=True)
    traj.write_text("{}")
    mtimes = iter([BASE, BASE + 5])
    with (
        patch.object(monitor, "_newest_trajectory_path", return_value=traj),
        patch.object(monitor, "_sample_size", return_value=100),
        patch.object(monitor, "_stat_mtime", side_effect=lambda p: next(mtimes)),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        assert monitor.trajectory_growing() is True


def test_trajectory_growing_false_when_no_file(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with patch.object(monitor, "_newest_trajectory_path", return_value=None):
        assert monitor.trajectory_growing() is False


# -- movement_recent_fine: two-sample append-only growth (TICKET-024/025) ---


def _two_sample_sizes(monitor: StallMonitor, sizes_by_path: dict):
    """Build a ``_sample_size`` side_effect that returns per-path first/second
    sizes (a two-sample sequence) for the paths in ``sizes_by_path``."""
    seqs = {path: iter(vals) for path, vals in sizes_by_path.items()}
    return lambda path: next(seqs[path])


def test_movement_recent_fine_true_when_gate_log_grows(tmp_path: Path) -> None:
    """TICKET-025: an append-only gate log that grows between samples is a
    genuine during-pass movement signal -> True."""
    monitor = _make_monitor(tmp_path)
    gate = tmp_path / "watched" / "ai" / "cycle-001-sentry-gate.md"
    with (
        patch.object(monitor, "_gate_log_path", return_value=gate),
        patch.object(monitor, "_sample_size", side_effect=_two_sample_sizes(monitor, {gate: [100, 200]})),
        patch.object(monitor, "_stat_mtime", return_value=BASE),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        assert monitor.movement_recent_fine() is True


def test_movement_recent_fine_true_when_cycles_out_mtime_advances(tmp_path: Path) -> None:
    """TICKET-025: cycles.out mtime advancing between samples -> True."""
    monitor = _make_monitor(tmp_path)
    cycles = tmp_path / "watched" / "cycles.out"
    cycles.parent.mkdir(parents=True, exist_ok=True)
    cycles.write_text("x")
    mtimes = iter([BASE, BASE + 5])
    with (
        patch.object(monitor, "_gate_log_path", return_value=None),
        patch.object(monitor, "_sample_size", return_value=100),
        patch.object(monitor, "_stat_mtime", side_effect=lambda p: next(mtimes)),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        assert monitor.movement_recent_fine() is True


def test_movement_recent_fine_false_when_flat(tmp_path: Path) -> None:
    """TICKET-025: no append activity (size + mtime flat) -> False."""
    monitor = _make_monitor(tmp_path)
    gate = tmp_path / "watched" / "ai" / "cycle-001-sentry-gate.md"
    with (
        patch.object(monitor, "_gate_log_path", return_value=gate),
        patch.object(monitor, "_sample_size", return_value=100),
        patch.object(monitor, "_stat_mtime", return_value=BASE),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        assert monitor.movement_recent_fine() is False


def test_movement_recent_fine_false_when_no_artifacts(tmp_path: Path) -> None:
    """TICKET-025: no append-only artifact present -> False (nothing to sample)."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_gate_log_path", return_value=None),
        patch.object(monitor, "_append_artifact_paths", return_value=[]),
    ):
        assert monitor.movement_recent_fine() is False


def test_movement_recent_fine_ignores_frozen_trajectory(tmp_path: Path) -> None:
    """TICKET-024: a frozen trajectory (written once at pass end) is NOT a
    movement signal; only the append-only artifacts count."""
    monitor = _make_monitor(tmp_path)
    gate = tmp_path / "watched" / "ai" / "cycle-001-sentry-gate.md"
    with (
        patch.object(monitor, "_gate_log_path", return_value=gate),
        patch.object(monitor, "_sample_size", return_value=100),
        patch.object(monitor, "_stat_mtime", return_value=BASE),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        # The trajectory path is never consulted by the fine-grained signal.
        assert monitor.movement_recent_fine() is False


# -- find_inner_pid / kill_inner: inner only, never the driver --------------


def test_find_inner_pid_returns_inner_not_driver(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    procs = [
        (111, "python3 /home/sasha/Research/four/examples/spokes/cycle-implementation.py --cycle 4"),
        (222, "bash /home/sasha/AI/sentry/run-cycles-v1.sh 1 3"),
    ]
    with (
        patch.object(monitor, "_scan_processes", return_value=procs),
        patch.object(monitor, "_ppid_map", return_value={}),
        patch.object(monitor, "_find_run_pid", return_value=None),
    ):
        assert monitor.find_inner_pid() == 111


def test_find_inner_pid_none_when_only_driver(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    procs = [(222, "bash /home/sasha/AI/sentry/run-cycles-v1.sh 1 3")]
    with patch.object(monitor, "_scan_processes", return_value=procs):
        assert monitor.find_inner_pid() is None


def test_find_inner_pid_returns_deepest_not_wrapper(tmp_path: Path) -> None:
    """TICKET-028: when the LLM bash shell, the wrapper, AND the inner spoke
    all match the marker, return the DEEPEST (child-most) PID -- the spoke."""
    monitor = _make_monitor(tmp_path)
    marker = "cycle-implementation.py"
    procs = [
        (100, f"bash -c 'python3 .../{marker} ...'"),   # LLM bash shell
        (200, f"timeout 3000 python3 .../{marker} ..."),  # wrapper
        (300, f"python3 .../{marker} --cycle 9"),         # inner spoke (deepest)
    ]
    # spoke(300) -> wrapper(200) -> shell(100) -> run.py(50)
    ppid_map = {300: 200, 200: 100, 100: 50}
    with (
        patch.object(monitor, "_scan_processes", return_value=procs),
        patch.object(monitor, "_ppid_map", return_value=ppid_map),
        patch.object(monitor, "_find_run_pid", return_value=50),
    ):
        assert monitor.find_inner_pid() == 300


def test_find_inner_pid_rejects_non_descendant_of_run_py(tmp_path: Path) -> None:
    """TICKET-029: a marker match that is NOT a descendant of the located
    run.py PID is rejected (find_inner_pid returns None)."""
    monitor = _make_monitor(tmp_path)
    marker = "cycle-implementation.py"
    procs = [(300, f"python3 .../{marker} --cycle 9")]
    # 300's parent chain does not reach run.py (50); it leads to 999 (absent).
    ppid_map = {300: 999}
    with (
        patch.object(monitor, "_scan_processes", return_value=procs),
        patch.object(monitor, "_ppid_map", return_value=ppid_map),
        patch.object(monitor, "_find_run_pid", return_value=50),
    ):
        assert monitor.find_inner_pid() is None


def test_find_inner_pid_accepts_descendant_of_run_py(tmp_path: Path) -> None:
    """TICKET-029: a marker match that IS a descendant of run.py is accepted."""
    monitor = _make_monitor(tmp_path)
    marker = "cycle-implementation.py"
    procs = [(300, f"python3 .../{marker} --cycle 9")]
    ppid_map = {300: 50}  # direct child of run.py (50)
    with (
        patch.object(monitor, "_scan_processes", return_value=procs),
        patch.object(monitor, "_ppid_map", return_value=ppid_map),
        patch.object(monitor, "_find_run_pid", return_value=50),
    ):
        assert monitor.find_inner_pid() == 300


def test_find_inner_pid_fallback_when_run_py_not_found(tmp_path: Path) -> None:
    """TICKET-029: when run.py cannot be located, fall back to the deepest
    marker match (do not silently kill, but do not lose the target)."""
    monitor = _make_monitor(tmp_path)
    marker = "cycle-implementation.py"
    procs = [
        (200, f"timeout 3000 python3 .../{marker} ..."),
        (300, f"python3 .../{marker} --cycle 9"),
    ]
    ppid_map = {300: 200}
    with (
        patch.object(monitor, "_scan_processes", return_value=procs),
        patch.object(monitor, "_ppid_map", return_value=ppid_map),
        patch.object(monitor, "_find_run_pid", return_value=None),
    ):
        assert monitor.find_inner_pid() == 300


def test_kill_inner_sends_sigterm_to_inner_pid(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_read_cmdline", return_value="python3 .../cycle-implementation.py"),
        patch.object(monitor, "_sleep", return_value=None),
        patch.object(monitor, "_is_alive", return_value=False),
        patch("sentry.stall.os.kill") as mock_kill,
    ):
        assert monitor.kill_inner(111) is True
    # SIGTERM to the inner pid, and no SIGKILL (process already dead).
    mock_kill.assert_called_once_with(111, signal.SIGTERM)


def test_kill_inner_refuses_driver_pid(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(
            monitor, "_read_cmdline", return_value="bash /home/sasha/AI/sentry/run-cycles-v1.sh"
        ),
        patch("sentry.stall.os.kill") as mock_kill,
    ):
        assert monitor.kill_inner(222) is False
    mock_kill.assert_not_called()


def test_kill_inner_returns_false_on_process_lookup_error(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_read_cmdline", return_value="python3 .../cycle-implementation.py"),
        patch("sentry.stall.os.kill", side_effect=ProcessLookupError),
    ):
        assert monitor.kill_inner(999) is False


def test_kill_inner_refuses_pid_without_spoke_marker(tmp_path: Path) -> None:
    """TICKET-030: kill_inner refuses a target whose cmdline does not contain
    the spoke marker (guards against a reused/stale PID)."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_read_cmdline", return_value="vim /tmp/notes.txt"),
        patch("sentry.stall.os.kill") as mock_kill,
    ):
        assert monitor.kill_inner(999) is False
    mock_kill.assert_not_called()


# -- handle_stall: decision + SentryLog entries -----------------------------


def _stalled_result() -> StallResult:
    return StallResult(
        stalled=True, last_movement=BASE, age_seconds=61 * 60, reason="stalled"
    )


def test_handle_stall_not_stalled_logs_probe(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with patch.object(
        monitor, "detect_stall", return_value=StallResult(stalled=False, reason="not stalled")
    ):
        decision = monitor.handle_stall()
    assert isinstance(decision, StallDecision)
    assert decision.action == "none"
    assert decision.logged is True
    events = [entry.event_type for entry in monitor.log.read_all()]
    assert "stall.probe" in events


def test_handle_stall_bare_estab_no_movement_kills(tmp_path: Path) -> None:
    """TICKET-022: a hung-but-ESTAB socket with no progress must NOT WAIT;
    it falls through to the KILL branch (crash detector, not stall detector)."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=True),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "kill"
    assert decision.pid == 111
    entries = monitor.log.read_all()
    kill_entries = [e for e in entries if e.event_type == "stall.kill"]
    assert len(kill_entries) == 1
    assert kill_entries[0].payload["pid"] == 111


def test_handle_stall_bare_estab_no_movement_no_pid_is_noop(tmp_path: Path) -> None:
    """TICKET-022: bare ESTAB + no progress + no inner pid -> safe no-op
    (never a WAIT on the socket alone)."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=True),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=None),
        patch("sentry.stall.os.kill") as mock_kill,
    ):
        decision = monitor.handle_stall()
    assert decision.action == "none"
    mock_kill.assert_not_called()


def test_handle_stall_estab_with_movement_waits(tmp_path: Path) -> None:
    """TICKET-022/025: ESTAB *corroborated* by a fresh fine-grained movement
    signal (an append-only artifact growing DURING the pass) is a legitimate
    WAIT."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=True),
        patch.object(monitor, "movement_recent_fine", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "wait"
    events = [entry.event_type for entry in monitor.log.read_all()]
    assert "stall.wait" in events


def test_handle_stall_waits_when_movement_recent(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "wait"
    events = [entry.event_type for entry in monitor.log.read_all()]
    assert "stall.wait" in events


def test_handle_stall_kills_inner_and_logs_kill(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "kill"
    assert decision.pid == 111
    entries = monitor.log.read_all()
    kill_entries = [e for e in entries if e.event_type == "stall.kill"]
    assert len(kill_entries) == 1
    assert kill_entries[0].payload["pid"] == 111


def test_handle_stall_kill_payload_carries_pid_and_cmdline(tmp_path: Path) -> None:
    """TICKET-031: the stall.kill payload carries the resolved PID AND its
    cmdline."""
    monitor = _make_monitor(tmp_path)
    cmdline = "python3 .../cycle-implementation.py --cycle 9"
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True),
        patch.object(monitor, "_read_cmdline", return_value=cmdline),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "kill"
    entries = monitor.log.read_all()
    kill_entries = [e for e in entries if e.event_type == "stall.kill"]
    assert len(kill_entries) == 1
    assert kill_entries[0].payload["pid"] == 111
    assert kill_entries[0].payload["cmdline"] == cmdline


def test_handle_stall_no_inner_pid_is_safe_noop(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=None),
        patch("sentry.stall.os.kill") as mock_kill,
    ):
        decision = monitor.handle_stall()
    assert decision.action == "none"
    mock_kill.assert_not_called()
    events = [entry.event_type for entry in monitor.log.read_all()]
    assert "stall.wait" in events


# -- inference-endpoint state signal (TICKET-032 / TICKET-036) --------------


def _endpoint_samples() -> tuple:
    """Two probe() samples with an advancing counter (generating)."""
    base = "http://192.168.1.157:8080"
    return (
        {base: {"reachable": True, "requests_processing": 1.0,
                "tokens_predicted_total": 100.0, "n_decode_total": 10.0,
                "predicted_tokens_seconds": 17.0}},
        {base: {"reachable": True, "requests_processing": 1.0,
                "tokens_predicted_total": 150.0, "n_decode_total": 15.0,
                "predicted_tokens_seconds": 17.0}},
    )


def test_handle_stall_inference_active_generating_waits(tmp_path: Path) -> None:
    """TICKET-036: requests_processing > 0 AND generating -> WAIT
    (healthy-slow), logged with endpoint evidence."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(
            monitor,
            "inference_active",
            return_value=(True, True, False, {"endpoint_samples": _endpoint_samples()}),
        ),
        patch("sentry.stall.os.kill") as mock_kill,
    ):
        decision = monitor.handle_stall()
    assert decision.action == "wait"
    assert decision.reason == "inference active + generating (healthy-slow)"
    mock_kill.assert_not_called()
    entries = monitor.log.read_all()
    wait_entries = [e for e in entries if e.event_type == "stall.wait"]
    assert len(wait_entries) == 1
    assert wait_entries[0].payload["reason"] == "inference active + generating (healthy-slow)"
    # Endpoint evidence is recorded in the payload.
    assert "endpoint" in wait_entries[0].payload
    assert "endpoint_samples" in decision.evidence


def test_handle_stall_blind_does_not_kill(tmp_path: Path) -> None:
    """TICKET-036: a blind endpoint (unreachable / /metrics unsupported) is
    NOT treated as wedged on that basis alone -> none, never a KILL."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(
            monitor,
            "inference_active",
            return_value=(False, False, True, {"endpoint_samples": _endpoint_samples()}),
        ),
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True) as mock_kill,
    ):
        decision = monitor.handle_stall()
    assert decision.action == "none"
    assert decision.reason == "endpoint blind - cannot confirm wedged"
    mock_kill.assert_not_called()
    entries = monitor.log.read_all()
    wait_entries = [e for e in entries if e.event_type == "stall.wait"]
    assert len(wait_entries) == 1
    assert wait_entries[0].payload["reason"] == "endpoint blind - cannot confirm wedged"


def test_handle_stall_active_not_generating_falls_through(tmp_path: Path) -> None:
    """TICKET-036: requests_processing > 0 but NOT generating (flat counters)
    -> existing path (kill) unchanged."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(
            monitor,
            "inference_active",
            return_value=(True, False, False, {"endpoint_samples": _endpoint_samples()}),
        ),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "kill"
    assert decision.pid == 111


# -- process-tree live-work signal (TICKET-033 / TICKET-039 / 040) ----------


def test_handle_stall_idle_live_child_waits(tmp_path: Path) -> None:
    """TICKET-040: LLM idle (no movement, endpoint not generating) + a live
    non-LLM child under the root -> WAIT (waiting on work, not wedged), with
    the sample cmdlines in the evidence. The kill path is NOT reached."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=50),
        patch.object(
            monitor, "has_live_work", return_value=(True, ["bash -c 'validator'"])
        ),
        patch.object(monitor, "find_inner_pid", return_value=111) as mock_find,
        patch.object(monitor, "kill_inner", return_value=True) as mock_kill,
    ):
        decision = monitor.handle_stall()
    assert decision.action == "wait"
    assert decision.reason == "live work in process tree (waiting on work, not wedged)"
    mock_find.assert_not_called()
    mock_kill.assert_not_called()
    # The sample cmdlines are recorded in the evidence / log payload.
    assert decision.evidence["proctree"]["samples"] == ["bash -c 'validator'"]
    entries = monitor.log.read_all()
    wait_entries = [e for e in entries if e.event_type == "stall.wait"]
    assert len(wait_entries) == 1
    assert wait_entries[0].payload["proctree"]["samples"] == ["bash -c 'validator'"]
    assert wait_entries[0].payload["proctree"]["root"] == 50


def test_handle_stall_idle_bare_tree_no_movement_kills(tmp_path: Path) -> None:
    """TICKET-040: LLM idle + bare tree (no live children) + no movement ->
    existing KILL path unchanged."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=50),
        patch.object(monitor, "has_live_work", return_value=(False, [])),
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "kill"
    assert decision.pid == 111
    entries = monitor.log.read_all()
    kill_entries = [e for e in entries if e.event_type == "stall.kill"]
    assert len(kill_entries) == 1
    assert kill_entries[0].payload["pid"] == 111


def test_handle_stall_no_root_falls_through_to_kill(tmp_path: Path) -> None:
    """TICKET-040: when the pipeline root cannot be resolved, the process-tree
    gate is skipped and the existing KILL path runs unchanged."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "detect_stall", return_value=_stalled_result()),
        patch.object(monitor, "any_socket_live", return_value=False),
        patch.object(monitor, "movement_recent_fine", return_value=False),
        patch.object(monitor, "inference_active", return_value=(False, False, False, {})),
        patch.object(monitor, "_resolve_pipeline_root", return_value=None),
        patch.object(monitor, "has_live_work", return_value=(True, ["bash"])) as mock_live,
        patch.object(monitor, "find_inner_pid", return_value=111),
        patch.object(monitor, "kill_inner", return_value=True),
    ):
        decision = monitor.handle_stall()
    assert decision.action == "kill"
    mock_live.assert_not_called()


def test_has_live_work_delegates_to_proctree(tmp_path: Path) -> None:
    """TICKET-040: the overridable has_live_work seam delegates to proctree."""
    monitor = _make_monitor(tmp_path)
    with patch.object(
        monitor.proctree, "has_live_work", return_value=(True, ["gh pr list"])
    ) as mock_pt:
        is_live, samples = monitor.has_live_work(50)
    assert is_live is True
    assert samples == ["gh pr list"]
    mock_pt.assert_called_once_with(50)


def test_resolve_pipeline_root_prefers_run_py(tmp_path: Path) -> None:
    """TICKET-040: _resolve_pipeline_root prefers the run.py PID."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_find_run_pid", return_value=50),
        patch.object(monitor, "find_inner_pid", return_value=111) as mock_find,
    ):
        assert monitor._resolve_pipeline_root() == 50
    mock_find.assert_not_called()


def test_resolve_pipeline_root_falls_back_to_inner_pid(tmp_path: Path) -> None:
    """TICKET-040: when run.py is not located, fall back to the deepest
    inner-spoke marker match."""
    monitor = _make_monitor(tmp_path)
    with (
        patch.object(monitor, "_find_run_pid", return_value=None),
        patch.object(monitor, "find_inner_pid", return_value=111),
    ):
        assert monitor._resolve_pipeline_root() == 111


def test_inference_active_returns_signal(tmp_path: Path) -> None:
    """TICKET-036: inference_active() takes two probe() samples with a sleep
    between and returns (rp_positive, generating, blind, evidence)."""
    monitor = _make_monitor(tmp_path)
    samples = _endpoint_samples()
    with (
        patch.object(monitor.endpoint_probe, "probe", side_effect=[samples[0], samples[1]]),
        patch.object(monitor, "_sleep", return_value=None) as mock_sleep,
    ):
        rp_positive, generating, blind, evidence = monitor.inference_active()
    assert rp_positive is True
    assert generating is True
    assert blind is False
    assert evidence["endpoint_samples"] == samples
    mock_sleep.assert_called_once()


def test_inference_active_blind_when_no_endpoint_reachable(tmp_path: Path) -> None:
    monitor = _make_monitor(tmp_path)
    blind_sample = {
        "http://192.168.1.157:8080": {
            "reachable": False, "requests_processing": None,
            "tokens_predicted_total": None, "n_decode_total": None,
            "predicted_tokens_seconds": None,
        }
    }
    with (
        patch.object(monitor.endpoint_probe, "probe", return_value=blind_sample),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        rp_positive, _generating, blind, _ = monitor.inference_active()
    assert rp_positive is False
    assert _generating is False
    assert blind is True


def test_inference_active_blind_when_metrics_unsupported(tmp_path: Path) -> None:
    """Reachable but requests_processing None (/metrics unsupported) -> blind."""
    monitor = _make_monitor(tmp_path)
    sample = {
        "http://192.168.1.157:8080": {
            "reachable": True, "requests_processing": None,
            "tokens_predicted_total": None, "n_decode_total": None,
            "predicted_tokens_seconds": None,
        }
    }
    with (
        patch.object(monitor.endpoint_probe, "probe", return_value=sample),
        patch.object(monitor, "_sleep", return_value=None),
    ):
        rp_positive, _generating, blind, _ = monitor.inference_active()
    assert rp_positive is False
    assert _generating is False
    assert blind is True


def test_endpoint_probe_base_urls_derived_from_endpoints(tmp_path: Path) -> None:
    """TICKET-036: the EndpointProbe base URLs are http://host:port derived
    from the socket endpoints."""
    monitor = _make_monitor(tmp_path)
    assert monitor.endpoint_probe.base_urls == (
        "http://192.168.1.157:8080",
        "http://192.168.1.161:8081",
    )




# -- real-artifact read-only test (skip gracefully if absent) ---------------


def test_detect_stall_against_real_sentry_artifacts(tmp_path: Path) -> None:
    real_dir = Path("/home/sasha/AI/sentry")
    if not (real_dir / "cycles.out").exists():
        pytest.skip("real sentry cycles.out not present")
    # Pass an explicit log under tmp_path so the watched dir stays read-only.
    monitor = StallMonitor(real_dir, log=SentryLog(tmp_path / "real-stall.log"))
    result = monitor.detect_stall()
    assert isinstance(result, StallResult)
    assert isinstance(result.stalled, bool)
