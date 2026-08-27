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

Kill target (TICKET-013 / TICKET-028 / TICKET-029 / TICKET-030): the inner
spoke is an LLM-spawned grandchild with no recorded PID. The real process
tree for a cycle is::

    bash run-cycles-v1.sh (driver)
      -> python3 run.py (outer LLM loop)
           -> bash -c 'python3 .../cycle-implementation.py ...' (LLM bash shell)
                -> timeout 3000 python3 .../cycle-implementation.py ... (wrapper)
                     -> python3 .../cycle-implementation.py ... (inner spoke)

The spoke marker (``cycle-implementation.py``) appears in the cmdline of the
LLM bash shell, the ``timeout``/``perl`` wrapper, AND the actual inner spoke,
so a first-match ``/proc`` scan can select the wrong layer. ``find_inner_pid``
therefore collects ALL non-driver marker matches and returns the **deepest**
(child-most, largest process-tree depth) one -- the actual spoke, not its
wrapper or shell (TICKET-028). The result is verified to be a **descendant of
the ``run.py`` PID** (the outer LLM loop, located by ``/proc`` pattern); a
marker match outside that subtree is rejected (TICKET-029). Before any
``os.kill``, ``kill_inner`` re-checks the target's cmdline: it must contain
the spoke marker AND not match a driver pattern (the driver HARD GUARD)
(TICKET-030). The resolved PID and its cmdline are logged in the
``stall.kill`` payload (TICKET-031). The driver (``run-cycles`` / ``run*.py``)
is NEVER a candidate.

Socket probe (TICKET-015 / TICKET-022 / TICKET-023): "socket live" means
an outbound ``ESTAB`` connection whose *peer* (destination) column is a
*remote* endpoint host:port (e.g. ``192.168.1.157:8080``). The match is
scoped to the peer column of the ``ss -tnp`` line, not a whole-line
substring, so a host:port token in the *local* address column or a process
path never counts as live. Local ``LISTEN`` lines are ignored.

Crash-vs-stall (TICKET-022): an ``ESTAB`` socket is a *crash* detector
(connection torn down), not a *stall* detector (connection alive but no
progress) — a hung LLM call stays ``ESTAB``. The socket probe therefore
NEVER triggers WAIT on its own; the WAIT decision is gated on the
trajectory / pass-cadence movement signal. A bare ``ESTAB`` with no
progress falls through to the KILL branch so a hung-but-ESTAB connection
cannot block a KILL forever.

Movement signal (TICKET-014 / TICKET-024 / TICKET-025): trajectories are
written ONCE, at full size, at the end of a pass (a single ``write_text``
in ``save_trajectory._emit``), so the newest trajectory on disk is the
previous pass's frozen output and never grows while a spoke is alive --
the old two-sample ``trajectory_growing()`` WAIT gate was therefore
structurally dead (TICKET-024). The WAIT gate is now gated on
``movement_recent_fine()``: a two-sample (size + mtime) check of the
append-only artifacts that actually grow DURING a pass -- the gate log
(``ai/*gate*.md``) and ``cycles.out``. These are appended incrementally
(each gate decision, each cycle marker), so a live, actively-working
spoke moves them while a hung pass does not. The socket probe remains a
crash detector only (TICKET-022): a bare ``ESTAB`` with no corroborating
fine-grained movement still falls through to KILL, so a hung-but-ESTAB
connection cannot block a KILL forever (TICKET-027).

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

from sentry.endpoint import EndpointProbe
from sentry.sentrylog import SentryLog

# Process patterns that identify a live driver (bash driver or python runner).
# The driver must NEVER be a kill candidate (TICKET-013).
_DRIVER_PATTERNS = (
    re.compile(r"run-cycles"),
    re.compile(r"run[\w.-]*\.py"),
)

# Marker for the inner spoke (the LLM-spawned grandchild, TICKET-013).
_INNER_SPOKE_MARKER = "cycle-implementation.py"

# The outer LLM loop (``run.py``) is the ancestor root for kill-targeting
# (TICKET-029): the inner spoke must be a descendant of it, not the driver.
_RUN_PY_RE = re.compile(r"run\.py")

# Column index of the peer (destination) field in ``ss -tnp`` output.
# The line shape is: State Recv-Q Send-Q Local Peer [Process], so the
# peer/destination is the 5th column (index 4). Matching this column --
# not the whole line -- is what keeps a host:port token in the *local*
# address column or a process path from false-positiving (TICKET-023).
_PEER_COL = 4


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
        # TICKET-036: inference-endpoint state signal (TICKET-032). Base
        # URLs are derived from the socket ``endpoints`` (host:port) as
        # http://host:port. No I/O happens here (EndpointProbe.__init__
        # stores state only).
        self.endpoint_probe = EndpointProbe(
            tuple(f"http://{endpoint}" for endpoint in self.endpoints),
            timeout=5.0,
        )
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
        AND its *peer* (destination) column equals the endpoint's
        ``host:port`` token. The match is scoped to the peer column
        (``_PEER_COL``), not a whole-line substring, so a host:port token in
        the *local* address column or a process path never counts as live
        (TICKET-023). Local ``LISTEN`` lines (state != ESTAB) are ignored
        (TICKET-015).
        """
        output = self._run_ss()
        result = {endpoint: False for endpoint in self.endpoints}
        for line in output.splitlines():
            fields = line.split()
            if not fields or fields[0] != "ESTAB":
                continue
            if len(fields) <= _PEER_COL:
                continue
            peer = fields[_PEER_COL]
            for endpoint in self.endpoints:
                if peer == endpoint:
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

    # -- fine-grained movement signal (TICKET-024 / TICKET-025) ------------

    def _gate_log_path(self) -> Path | None:
        """Locate the append-only gate log under the project dir. Overridable.

        Mirrors :meth:`sentry.sentinel.Sentinel._find_gate_log`: the gate log
        is an append-only ``ai/*gate*.md`` artifact that grows as gate
        decisions are logged.
        """
        candidates: list[Path] = [
            self.project_dir / "ai" / "cycle-001-sentry-gate.md"
        ]
        for pattern in ("ai/*gate*.md", "*gate*.md"):
            candidates.extend(sorted(self.project_dir.glob(pattern)))
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    def _append_artifact_paths(self) -> list[Path]:
        """Append-only artifacts that grow DURING a pass (gate log, cycles.out).

        Unlike trajectories (written once at pass end), these are appended
        incrementally, so their two-sample growth is a genuine "the spoke is
        working right now" signal (TICKET-025).
        """
        paths: list[Path] = []
        gate = self._gate_log_path()
        if gate is not None:
            paths.append(gate)
        try:
            if self.cycles_out_path.is_file():
                paths.append(self.cycles_out_path)
        except OSError:
            pass
        return paths

    def movement_recent_fine(self, sample_interval: float = 0.5) -> bool:
        """True when an append-only artifact moved between two samples.

        Samples the size and mtime of each append-only artifact (gate log and
        ``cycles.out``) twice with a short sleep between. Returns True when any
        artifact's size grew OR its mtime advanced; False when there is no
        append-only artifact. This is the WAIT-gate movement signal that
        replaces the structurally-dead ``trajectory_growing()`` gate
        (TICKET-024/025): it reflects append activity that happens DURING a
        pass, which the coarse single-sample ``detect_stall`` (aged by
        ``stall_seconds``) cannot see.
        """
        paths = self._append_artifact_paths()
        if not paths:
            return False
        first = {
            path: (self._sample_size(path), self._stat_mtime(path))
            for path in paths
        }
        self._sleep(sample_interval)
        for path in paths:
            first_size, first_mtime = first[path]
            second_size = self._sample_size(path)
            second_mtime = self._stat_mtime(path)
            if second_size > first_size:
                return True
            if (
                first_mtime is not None
                and second_mtime is not None
                and second_mtime > first_mtime
            ):
                return True
        return False

    # -- inference-endpoint state signal (TICKET-032 / TICKET-036) -----------

    def inference_active(self, sample_interval: float = 0.5) -> tuple[bool, bool, bool, dict]:
        """Return the inference-endpoint state signal.

        Returns ``(requests_processing_positive, generating, blind, evidence)``:

        * ``requests_processing_positive``: True when any endpoint reports
          ``requests_processing > 0`` (a request is in flight).
        * ``generating``: True when a generation counter advanced between the
          two samples (TICKET-035) — the inference is actually advancing.
        * ``blind``: True when no endpoint is reachable, OR every reachable
          endpoint has ``requests_processing`` None (``/metrics`` unsupported,
          so the endpoint is up but blind to inference state).
        * ``evidence``: the two ``probe()`` samples (for the log payload).

        Takes two ``self.endpoint_probe.probe()`` samples with ``self._sleep``
        between them (the sampling is owned here, mirroring
        ``movement_recent_fine``); the pure comparison is
        ``EndpointProbe.generating``.
        """
        first = self.endpoint_probe.probe()
        self._sleep(sample_interval)
        second = self.endpoint_probe.probe()
        samples = (first, second)

        requests_processing_positive = False
        for entry in first.values():
            if not isinstance(entry, dict):
                continue
            rp = entry.get("requests_processing")
            if isinstance(rp, float) and rp > 0:
                requests_processing_positive = True
                break
        generating = self.endpoint_probe.generating(samples)

        reachable = [
            entry for entry in first.values()
            if isinstance(entry, dict) and entry.get("reachable")
        ]
        if not reachable:
            blind = True
        else:
            blind = all(
                entry.get("requests_processing") is None for entry in reachable
            )

        evidence = {"endpoint_samples": samples}
        return requests_processing_positive, generating, blind, evidence

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

    def _ppid_map(self) -> dict[int, int]:
        """Return ``{pid: ppid}`` for live processes. Overridable in tests.

        Reads field 4 (ppid) of ``/proc/<pid>/stat``. The comm field (field 2)
        is parenthesized and may contain spaces, so the parse splits on the
        *last* ``)`` to skip past it; the ppid is then the second whitespace
        field after it (field 3 = state, field 4 = ppid).
        """
        result: dict[int, int] = {}
        try:
            entries = os.listdir("/proc")
        except OSError:
            return result
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "rb") as handle:
                    raw = handle.read()
            except OSError:
                continue
            text = raw.decode("utf-8", "replace")
            close = text.rfind(")")
            if close == -1:
                continue
            fields = text[close + 1 :].split()
            if len(fields) >= 2:
                try:
                    result[pid] = int(fields[1])
                except ValueError:
                    continue
        return result

    def _process_depth(self, pid: int, ppid_map: dict[int, int]) -> int:
        """Depth of ``pid`` in the process tree (child-most = largest).

        Counts ancestor hops up the ``ppid_map`` chain; cycle-guarded so a
        malformed map cannot loop forever.
        """
        depth = 0
        seen: set[int] = set()
        current = pid
        while current in ppid_map:
            if current in seen:
                break
            seen.add(current)
            parent = ppid_map[current]
            depth += 1
            if parent == current or parent not in ppid_map:
                break
            current = parent
        return depth

    def _find_run_pid(self) -> int | None:
        """Locate the outer LLM-loop ``run.py`` PID (not the driver).

        Matches a cmdline containing ``run.py`` while excluding the driver
        (``run-cycles``). Returns None when no outer loop is found.
        Overridable in tests.
        """
        for pid, cmdline in self._scan_processes():
            if "run-cycles" in cmdline:
                continue
            if _RUN_PY_RE.search(cmdline):
                return pid
        return None

    def _is_descendant_of(
        self, pid: int, ancestor: int, ppid_map: dict[int, int]
    ) -> bool:
        """True when ``pid`` is a strict descendant of ``ancestor``.

        Walks the ``ppid_map`` chain up from ``pid``; cycle-guarded.
        """
        if pid == ancestor:
            return False
        current = pid
        seen: set[int] = set()
        while current in ppid_map:
            if current in seen:
                return False
            seen.add(current)
            parent = ppid_map[current]
            if parent == ancestor:
                return True
            if parent == current or parent not in ppid_map:
                return False
            current = parent
        return False

    def find_inner_pid(self) -> int | None:
        """Locate the inner-spoke PID to kill (TICKET-013 / TICKET-028 / 029).

        Collects ALL non-driver ``/proc`` cmdline matches for the spoke marker
        (``cycle-implementation.py``) and returns the **deepest** (child-most)
        one -- the actual spoke, not the ``timeout``/``perl`` wrapper or the
        LLM bash-tool shell (TICKET-028). The result is then verified to be a
        **descendant of the ``run.py`` PID** (the outer LLM loop); a marker
        match outside that subtree is rejected (TICKET-029). Driver processes
        (``run-cycles`` / ``run*.py``) are never candidates. When the ``run.py``
        PID cannot be located, falls back to the deepest marker match. Returns
        None when no inner spoke is found.
        """
        candidates: list[int] = []
        for pid, cmdline in self._scan_processes():
            if self._is_driver_cmdline(cmdline):
                continue
            if _INNER_SPOKE_MARKER in cmdline:
                candidates.append(pid)
        if not candidates:
            return None
        ppid_map = self._ppid_map()
        deepest = max(candidates, key=lambda p: self._process_depth(p, ppid_map))
        run_pid = self._find_run_pid()
        if run_pid is not None and not self._is_descendant_of(
            deepest, run_pid, ppid_map
        ):
            return None
        return deepest

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
        # TICKET-030: verify the target's cmdline actually contains the spoke
        # marker before killing (guards against a reused/stale PID).
        if cmdline is None or _INNER_SPOKE_MARKER not in cmdline:
            return False
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
        # TICKET-022 / TICKET-027: an ESTAB socket is a *crash* detector, not
        # a *stall* detector (a hung LLM call stays ESTAB). The socket probe
        # therefore never triggers WAIT on its own; WAIT is gated on the
        # corroborating fine-grained movement signal (an append-only artifact
        # growing DURING the pass, TICKET-024/025). A bare ESTAB with no
        # movement falls through to the KILL branch so a hung-but-ESTAB
        # connection cannot block a KILL forever.
        socket_live = self.any_socket_live()
        moving = self.movement_recent_fine()
        evidence = dict(result.evidence)
        evidence["socket_live"] = socket_live
        evidence["movement_recent"] = moving
        if moving:
            self.log.append(
                "stall.wait",
                {
                    "reason": "append-only artifact moving during pass",
                    "socket_live": socket_live,
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="wait",
                reason="append-only artifact moving during pass",
                evidence=evidence,
                logged=True,
            )
        # TICKET-036: inference-endpoint state signal (TICKET-032). A
        # healthy-slow inference (a request in flight AND the generation
        # counters advancing) is WAIT, not a wedge. A blind endpoint
        # (unreachable, or /metrics unsupported so requests_processing is
        # None) must NOT be treated as wedged on that basis alone: prefer
        # WAIT/none over KILL so a probe outage cannot masquerade as a wedge.
        requests_processing_positive, generating, blind, endpoint_evidence = (
            self.inference_active()
        )
        evidence.update(endpoint_evidence)
        if requests_processing_positive and generating:
            self.log.append(
                "stall.wait",
                {
                    "reason": "inference active + generating (healthy-slow)",
                    "socket_live": socket_live,
                    "endpoint": endpoint_evidence,
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="wait",
                reason="inference active + generating (healthy-slow)",
                evidence=evidence,
                logged=True,
            )
        if blind:
            self.log.append(
                "stall.wait",
                {
                    "reason": "endpoint blind - cannot confirm wedged",
                    "socket_live": socket_live,
                    "endpoint": endpoint_evidence,
                    "last_movement": result.last_movement,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="none",
                reason="endpoint blind - cannot confirm wedged",
                evidence=evidence,
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
                evidence=evidence,
                logged=True,
            )
        killed = self.kill_inner(pid)
        if killed:
            self.log.append(
                "stall.kill",
                {
                    "pid": pid,
                    "cmdline": self._read_cmdline(pid),
                    "reason": result.reason,
                    "age_seconds": result.age_seconds,
                },
            )
            return StallDecision(
                action="kill",
                reason=result.reason,
                pid=pid,
                evidence=evidence,
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
            evidence=evidence,
            logged=True,
        )
