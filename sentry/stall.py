"""StallMonitor: detect a no-movement stall and decide WAIT vs KILL.

Cycle 4 stall-handling for the sentry supervisor. Watches one project
directory (a path like ``/home/sasha/AI/<name>`` containing ``cycles.out``
and ``ai/trajectories/``) and, when no movement has been observed for
``stall_seconds``, decides whether to WAIT (a live signal is present) or
KILL the stalled inner spoke. Every decision is appended to the
:class:`~sentry.sentrylog.SentryLog`.

Timer reconciliation (TICKET-012): the driver kills its inner spoke at
``--inner-seconds 3000`` (50 min) and ``run.py`` at ``perl alarm 3600``
(60 min). A stall monitor must fire *before* those timeouts to act on a
live spoke, so the threshold is a constructor parameter (``stall_seconds``)
rather than a hardcoded 60 min.

Kill target (TICKET-013): the inner spoke is an LLM-spawned grandchild with
no recorded PID; it is located by pattern-matching ``/proc`` for the spoke
marker (``cycle-implementation.py``). The driver (``run-cycles`` /
``run*.py``) is NEVER a candidate and is re-checked before any kill.

Socket probe (TICKET-015): "socket live" means an outbound ``ESTAB``
connection to a *remote* endpoint host:port (e.g. ``192.168.1.157:8080``),
not a local ``LISTEN`` on the same port. Local ``LISTEN`` lines are ignored.
An ``ESTAB`` socket is a *crash* detector, not a *stall* detector (a hung
LLM call stays ``ESTAB``), so the decision also weighs trajectory growth.

Trajectory growth (TICKET-014): trajectories are written once at pass end,
so "growing" is a weak WAIT signal; it is sampled twice (size + mtime) and
combined with the socket probe rather than relied on alone.

The watched project dir is READ-ONLY: the only write this module performs is
to the :class:`~sentry.sentrylog.SentryLog` path (and the sentry repo).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from sentry.sentrylog import SentryLog

# Process patterns that identify a live driver (bash driver or python runner).
# The driver must NEVER be a kill candidate (TICKET-013).
_DRIVER_PATTERNS = (
    re.compile(r"run-cycles"),
    re.compile(r"run[\w.-]*\.py"),
)

# Marker for the inner spoke (the LLM-spawned grandchild, TICKET-013).
_INNER_SPOKE_MARKER = "cycle-implementation.py"


@dataclass
class StallResult:
    """Outcome of a single stall-detection probe.

    Attributes:
        stalled: True when no movement has been observed for ``stall_seconds``.
        last_movement: Newest movement timestamp (unix seconds), if any.
        age_seconds: ``now - last_movement``, if ``last_movement`` is known.
        reason: Human-readable explanation.
        evidence: Structured context for logging/debugging.
    """

    stalled: bool
    last_movement: float | None = None
    age_seconds: float | None = None
    reason: str = ""
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "stalled": self.stalled,
            "last_movement": self.last_movement,
            "age_seconds": self.age_seconds,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"StallResult(stalled={self.stalled!r}, "
            f"last_movement={self.last_movement!r}, "
            f"age_seconds={self.age_seconds!r}, reason={self.reason!r})"
        )


@dataclass
class StallDecision:
    """Outcome of a stall-handling decision.

    Attributes:
        action: One of ``"none"``, ``"wait"``, ``"kill"``.
        reason: Human-readable explanation.
        pid: The inner-spoke PID acted on, if any.
        evidence: Structured context for logging/debugging.
        logged: True when a decision entry was appended to the SentryLog.
    """

    action: str
    reason: str = ""
    pid: int | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    logged: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "pid": self.pid,
            "evidence": dict(self.evidence),
            "logged": self.logged,
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"StallDecision(action={self.action!r}, reason={self.reason!r}, "
            f"pid={self.pid!r}, logged={self.logged!r})"
        )


class StallMonitor:
    """Detect a no-movement stall and decide WAIT vs KILL for one project dir."""

    def __init__(
        self,
        project_dir: str | os.PathLike,
        *,
        log: SentryLog | None = None,
        stall_seconds: float = 3600.0,
        endpoints: tuple[str, ...] = (
            "192.168.1.157:8080",
            "192.168.1.161:8081",
        ),
        trajectories_dir: str | os.PathLike | None = None,
        git_dir: str | os.PathLike | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.stall_seconds = stall_seconds
        self.endpoints = tuple(endpoints)
        self.trajectories_dir = (
            Path(trajectories_dir)
            if trajectories_dir is not None
            else self.project_dir / "ai" / "trajectories"
        )
        self.git_dir = (
            Path(git_dir) if git_dir is not None else self.project_dir / "proj"
        )
        self.cycles_out_path = self.project_dir / "cycles.out"
        if log is not None:
            self.log = log
        else:
            default_log_path = self.project_dir / "ai" / "sentry-stall.log"
            default_log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log = SentryLog(default_log_path)

    # -- movement signals ---------------------------------------------------

    def _git_commit_time(self) -> float | None:
        """Newest git commit time (unix seconds) in ``git_dir``. Overridable."""
        try:
            proc = subprocess.run(
                ["git", "log", "-1", "--format=%ct"],
                cwd=str(self.git_dir),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        if not out:
            return None
        try:
            return float(out.splitlines()[0])
        except ValueError:
            return None

    def _branch_ref_time(self) -> float | None:
        """Newest branch-ref mtime under ``git_dir/.git/refs/heads``.

        Scans recursively for files and takes the max mtime. Overridable.
        """
        refs_dir = self.git_dir / ".git" / "refs" / "heads"
        if not refs_dir.is_dir():
            return None
        newest: float | None = None
        for path in refs_dir.rglob("*"):
            if not path.is_file():
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mtime > newest:
                newest = mtime
        return newest

    def _cycles_out_mtime(self) -> float | None:
        """Mtime of ``cycles.out``. Overridable."""
        try:
            return self.cycles_out_path.stat().st_mtime
        except OSError:
            return None

    def _newest_trajectory_mtime(self) -> float | None:
        """Mtime of the newest ``trajectory_*.json``. Overridable."""
        path = self._newest_trajectory_path()
        if path is None:
            return None
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def detect_stall(self, now: float | None = None) -> StallResult:
        """Detect a no-movement stall from the newest movement signal.

        ``last_movement`` is the max of the git commit time, newest branch-ref
        mtime, ``cycles.out`` mtime, and newest trajectory mtime. ``stalled``
        is True when ``now - last_movement >= stall_seconds``. When no movement
        signal is found, ``stalled`` is False with reason ``no movement signal``.
        """
        if now is None:
            now = time.time()
        git_commit = self._git_commit_time()
        branch_ref = self._branch_ref_time()
        cycles_out = self._cycles_out_mtime()
        trajectory = self._newest_trajectory_mtime()
        evidence: dict[str, object] = {
            "git_commit_time": git_commit,
            "branch_ref_time": branch_ref,
            "cycles_out_mtime": cycles_out,
            "newest_trajectory_mtime": trajectory,
            "stall_seconds": self.stall_seconds,
        }
        present = [
            value
            for value in (git_commit, branch_ref, cycles_out, trajectory)
            if value is not None
        ]
        if not present:
            return StallResult(
                stalled=False,
                last_movement=None,
                age_seconds=None,
                reason="no movement signal",
                evidence=evidence,
            )
        last_movement = max(present)
        age_seconds = now - last_movement
        stalled = age_seconds >= self.stall_seconds
        reason = "stalled" if stalled else "not stalled"
        return StallResult(
            stalled=stalled,
            last_movement=last_movement,
            age_seconds=age_seconds,
            reason=reason,
            evidence=evidence,
        )

    # -- socket probe -------------------------------------------------------

    def _run_ss(self) -> str:
        """Run ``ss -tnp`` and return stdout ('' on failure). Overridable."""
        try:
            proc = subprocess.run(
                ["ss", "-tnp"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout

    def probe_sockets(self) -> dict[str, bool]:
        """Map each endpoint to whether an outbound ESTAB connection is live.

        A line is a live endpoint connection when its state field is ``ESTAB``
        AND the line contains the endpoint's ``host:port`` token. Local
        ``LISTEN`` lines (state != ESTAB) are ignored (TICKET-015).
        """
        output = self._run_ss()
        result = {endpoint: False for endpoint in self.endpoints}
        for line in output.splitlines():
            fields = line.split()
            if not fields or fields[0] != "ESTAB":
                continue
            for endpoint in self.endpoints:
                if endpoint in line:
                    result[endpoint] = True
        return result

    def any_socket_live(self) -> bool:
        """True when any endpoint has a live outbound ESTAB connection."""
        return any(self.probe_sockets().values())

    # -- trajectory growth --------------------------------------------------

    def _newest_trajectory_path(self) -> Path | None:
        """Newest ``trajectory_*.json`` in ``trajectories_dir``. Overridable."""
        if not self.trajectories_dir.is_dir():
            return None
        candidates = [
            path
            for path in self.trajectories_dir.glob("trajectory_*.json")
            if path.is_file()
        ]
        if not candidates:
            return None

        def _mtime(path: Path) -> float:
            try:
                return path.stat().st_mtime
            except OSError:
                return 0.0

        return max(candidates, key=_mtime)

    def _sample_size(self, path: Path) -> int:
        """Size of ``path`` in bytes (0 on error). Overridable."""
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    def _stat_mtime(self, path: Path) -> float | None:
        """Mtime of ``path`` (None on error). Overridable."""
        try:
            return path.stat().st_mtime
        except OSError:
            return None

    def _sleep(self, seconds: float) -> None:
        """Sleep ``seconds``. Overridable in tests."""
        time.sleep(seconds)

    def trajectory_growing(self, sample_interval: float = 0.5) -> bool:
        """True when the newest trajectory grew between two samples.

        Samples size (and mtime) twice with a short sleep between. Returns
        True when the second size exceeds the first OR the mtime advanced.
        Returns False when there is no trajectory file.
        """
        path = self._newest_trajectory_path()
        if path is None:
            return False
        first_size = self._sample_size(path)
        first_mtime = self._stat_mtime(path)
        self._sleep(sample_interval)
        second_size = self._sample_size(path)
        second_mtime = self._stat_mtime(path)
        size_grew = second_size > first_size
        mtime_advanced = (
            first_mtime is not None
            and second_mtime is not None
            and second_mtime > first_mtime
        )
        return size_grew or mtime_advanced

    # -- process scan / kill ------------------------------------------------

    def _scan_processes(self) -> list[tuple[int, str]]:
        """Return ``(pid, cmdline)`` for live processes. Overridable in tests."""
        matches: list[tuple[int, str]] = []
        my_pid = os.getpid()
        try:
            entries = os.listdir("/proc")
        except OSError:
            return matches
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == my_pid:
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            if cmdline:
                matches.append((pid, cmdline))
        return matches

    def _is_driver_cmdline(self, cmdline: str) -> bool:
        """True when ``cmdline`` matches a driver pattern."""
        return any(pattern.search(cmdline) for pattern in _DRIVER_PATTERNS)

    def find_inner_pid(self) -> int | None:
        """Locate the inner-spoke PID by pattern-matching ``/proc``.

        Returns the first non-driver process whose cmdline contains the inner
        spoke marker (``cycle-implementation.py``). Driver processes
        (``run-cycles`` / ``run*.py``) are never candidates (TICKET-013).
        Returns None when no inner spoke is found.
        """
        for pid, cmdline in self._scan_processes():
            if self._is_driver_cmdline(cmdline):
                continue
            if _INNER_SPOKE_MARKER in cmdline:
                return pid
        return None

    def _read_cmdline(self, pid: int) -> str | None:
        """Read ``/proc/<pid>/cmdline`` (None on error). Overridable."""
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
        return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()

    def _is_alive(self, pid: int) -> bool:
        """True when ``pid`` is alive. Overridable in tests."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def kill_inner(self, pid: int) -> bool:
        """Kill the inner spoke ``pid`` (never the driver).

        HARD GUARD: if ``/proc/<pid>/cmdline`` matches a driver pattern,
        refuse and return False. Otherwise send SIGTERM; if the process is
        still alive after a short wait, send SIGKILL. Never uses a
        process-group kill (no ``os.killpg``). Returns True on success,
        False on ProcessLookupError/OSError.
        """
        cmdline = self._read_cmdline(pid)
        if cmdline is not None and self._is_driver_cmdline(cmdline):
            return False  # HARD GUARD: never the driver
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return False
        self._sleep(0.2)
        if self._is_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        return True

    # -- decision -----------------------------------------------------------

    def handle_stall(self, now: float | None = None) -> StallDecision:
        """Orchestrate a stall probe and WAIT/KILL decision.

        Always appends exactly one decision entry to the SentryLog.
        """
        result = self.detect_stall(now)
        if not result.stalled:
            self.log.append(
                "stall.probe",
                {
                    "stalled": False,
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                    "reason": result.reason,
                },
            )
            return StallDecision(
                action="none",
                reason=result.reason,
                evidence=dict(result.evidence),
                logged=True,
            )
        if self.any_socket_live():
            self.log.append(
                "stall.wait",
                {
                    "reason": "socket live",
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="wait",
                reason="socket live",
                evidence=dict(result.evidence),
                logged=True,
            )
        if self.trajectory_growing():
            self.log.append(
                "stall.wait",
                {
                    "reason": "trajectory growing",
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="wait",
                reason="trajectory growing",
                evidence=dict(result.evidence),
                logged=True,
            )
        pid = self.find_inner_pid()
        if pid is None:
            self.log.append(
                "stall.wait",
                {
                    "reason": "stalled but no inner pid found",
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="none",
                reason="stalled but no inner pid found",
                evidence=dict(result.evidence),
                logged=True,
            )
        killed = self.kill_inner(pid)
        if killed:
            self.log.append(
                "stall.kill",
                {
                    "pid": pid,
                    "reason": result.reason,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="kill",
                reason=result.reason,
                pid=pid,
                evidence=dict(result.evidence),
                logged=True,
            )
        self.log.append(
            "stall.kill_noop",
            {
                "pid": pid,
                "reason": result.reason,
                "age_seconds": result.age_seconds,
            },
        )
        return StallDecision(
            action="none",
            reason=result.reason,
            pid=pid,
            evidence=dict(result.evidence),
            logged=True,
        )
