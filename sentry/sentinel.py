"""Sentinel: deterministic, stdlib-only detection of driver death and wall-kill.

Watches one project directory (a path like ``/home/sasha/AI/<name>`` containing
``cycles.out``, the gate log, and the ``run-cycles.sh`` driver) and reports
whether a rescue action is warranted.

Marker grammar (tolerant parsing, see TICKET-005):
  * start: ``========== CYCLE <n>  <date> ==========``  (``=`` framing, any spacing)
  * done:  ``========== CYCLE <n> done ==========`
  * header lines beginning with ``#`` (e.g. ``# endpoint: ...``) are ignored.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# A done marker: ``=+ CYCLE <n> done`` (``done`` immediately after the number).
_DONE_RE = re.compile(r"^=+\s*CYCLE\s+(\d+)\s+done\b")
# A start marker: ``=+ CYCLE <n>`` (number not followed by ``done``).
_START_RE = re.compile(r"^=+\s*CYCLE\s+(\d+)\b")
# A gate-log cycle heading: ``## Cycle <n> ...``.
_GATE_CYCLE_RE = re.compile(r"^##\s+Cycle\s+(\d+)\b")

# Process patterns that identify a live driver (bash driver or python runner).
_DRIVER_PATTERNS = (
    re.compile(r"run-cycles"),
    re.compile(r"run[\w.-]*\.py"),
)


@dataclass
class DetectionResult:
    """Outcome of a single detection probe.

    Attributes:
        detected: True when a rescue condition is present.
        cycle: The cycle number implicated, if any.
        reason: Human-readable explanation.
        evidence: Structured context for logging/debugging.
    """

    detected: bool
    cycle: int | None = None
    reason: str = ""
    evidence: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "detected": self.detected,
            "cycle": self.cycle,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"DetectionResult(detected={self.detected!r}, cycle={self.cycle!r}, "
            f"reason={self.reason!r})"
        )


class Sentinel:
    """Detects driver death and wall-kill-without-merge for one project dir."""

    def __init__(
        self,
        project_dir: str | os.PathLike,
        cycles_out: str | os.PathLike | None = None,
        gate_log: str | os.PathLike | None = None,
        driver: str | os.PathLike | None = None,
    ) -> None:
        self.project_dir = Path(project_dir)
        self.cycles_out_path = (
            Path(cycles_out) if cycles_out is not None else self.project_dir / "cycles.out"
        )
        self.gate_log_path = (
            Path(gate_log) if gate_log is not None else self._find_gate_log()
        )
        self.driver_path = (
            Path(driver) if driver is not None else self.project_dir / "run-cycles.sh"
        )

    # -- path discovery -----------------------------------------------------

    def _find_gate_log(self) -> Path | None:
        """Locate the gate log under the project dir, if present."""
        candidates: list[Path] = [self.project_dir / "ai" / "cycle-001-sentry-gate.md"]
        for pattern in ("ai/*gate*.md", "*gate*.md", "**/*gate*.md"):
            candidates.extend(sorted(self.project_dir.glob(pattern)))
        for candidate in candidates:
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
        return None

    # -- cycles.out parsing -------------------------------------------------

    def _parse_cycles(self) -> tuple[set[int], set[int]]:
        """Return ``(started, done)`` cycle-number sets parsed from cycles.out."""
        started: set[int] = set()
        done: set[int] = set()
        path = self.cycles_out_path
        if path is None or not path.exists():
            return started, done
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return started, done
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            done_match = _DONE_RE.match(stripped)
            if done_match:
                done.add(int(done_match.group(1)))
                continue
            start_match = _START_RE.match(stripped)
            if start_match:
                started.add(int(start_match.group(1)))
        return started, done

    def get_in_flight_cycles(self) -> list[int]:
        """Cycles with a start marker but no done marker, ascending."""
        started, done = self._parse_cycles()
        return sorted(started - done)

    def get_first_not_done_cycle(self) -> int | None:
        """Smallest in-flight cycle, or None when nothing is in flight."""
        in_flight = self.get_in_flight_cycles()
        return in_flight[0] if in_flight else None

    # -- gate log -----------------------------------------------------------

    def has_gate_block(self, cycle: int) -> bool:
        """True when the gate log has a non-pending ``## Cycle <n>`` block."""
        path = self.gate_log_path
        if path is None or not path.exists():
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        for line in text.splitlines():
            match = _GATE_CYCLE_RE.match(line.strip())
            if match and int(match.group(1)) == cycle and "pending" not in line.lower():
                return True
        return False

    # -- process liveness ---------------------------------------------------

    def _matches_driver(self, cmdline: str) -> bool:
        return any(pattern.search(cmdline) for pattern in _DRIVER_PATTERNS)

    def _scan_driver_processes(self) -> list[tuple[int, str]]:
        """Return ``(pid, cmdline)`` for live processes matching driver patterns.

        Scans ``/proc`` (stdlib only). Excludes this process. Overridable in
        tests via ``patch.object(instance, "_scan_driver_processes")``.
        """
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
            if cmdline and self._matches_driver(cmdline):
                matches.append((pid, cmdline))
        return matches

    def is_driver_process_alive(self) -> bool:
        """True when any ``run-cycles`` or ``run*.py`` process is alive."""
        return len(self._scan_driver_processes()) > 0

    # -- detection probes ---------------------------------------------------

    def detect_driver_death(self) -> DetectionResult:
        """A cycle in flight with no driver process alive."""
        in_flight = self.get_in_flight_cycles()
        if not in_flight:
            return DetectionResult(False, reason="no cycle in flight")
        if self.is_driver_process_alive():
            return DetectionResult(
                False, cycle=in_flight[0], reason="driver process alive"
            )
        return DetectionResult(
            True,
            cycle=in_flight[0],
            reason="driver death: cycle in flight, no driver process alive",
            evidence={"in_flight": in_flight},
        )

    def detect_wall_kill_no_merge(self) -> DetectionResult:
        """A start marker with no done marker, no gate block, and no live process."""
        in_flight = self.get_in_flight_cycles()
        for cycle in in_flight:
            if self.has_gate_block(cycle):
                continue  # already merged/logged
            if self.is_driver_process_alive():
                continue  # live, not wall-killed
            return DetectionResult(
                True,
                cycle=cycle,
                reason="wall-kill without merge",
                evidence={"cycle": cycle, "in_flight": in_flight},
            )
        return DetectionResult(False, reason="no wall-kill-without-merge detected")
