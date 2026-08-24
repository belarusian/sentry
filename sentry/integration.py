"""Integrator: read-only, thin wrapper that runs a Sentinel against real
project artifacts and returns a structured per-cycle summary.

The Integrator is READ-ONLY: it only reads the two watched paths
(``cycles.out`` and the gate log) and never creates, modifies, or deletes
anything under the watched project (TICKET-007).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sentry.sentinel import Sentinel


@dataclass
class IntegrationSummary:
    """Aggregate per-cycle detection summary for one watched project.

    Attributes:
        started: Cycle numbers with a start marker.
        done: Cycle numbers with a done marker.
        in_flight: Started-but-not-done cycles, ascending.
        wall_kill_candidates: In-flight cycles with no gate block (ascending).
        gate_blocks: Cycle numbers with a non-pending gate block.
        first_not_done: Smallest in-flight cycle, or None when none in flight.
    """

    started: set[int] = field(default_factory=set)
    done: set[int] = field(default_factory=set)
    in_flight: list[int] = field(default_factory=list)
    wall_kill_candidates: list[int] = field(default_factory=list)
    gate_blocks: set[int] = field(default_factory=set)
    first_not_done: int | None = None

    def to_dict(self) -> dict[str, object]:
        """JSON-friendly view (sets rendered as sorted lists)."""
        return {
            "started": sorted(self.started),
            "done": sorted(self.done),
            "in_flight": list(self.in_flight),
            "wall_kill_candidates": list(self.wall_kill_candidates),
            "gate_blocks": sorted(self.gate_blocks),
            "first_not_done": self.first_not_done,
        }


class Integrator:
    """Read-only layer that runs a Sentinel against real project artifacts.

    Wraps a :class:`~sentry.sentinel.Sentinel` configured to read exactly the
    two watched paths. ``summary()`` aggregates the per-cycle detection state
    into an :class:`IntegrationSummary`.
    """

    def __init__(
        self,
        cycles_out: str | os.PathLike,
        gate_log: str | os.PathLike | None = None,
        project_dir: str | os.PathLike | None = None,
    ) -> None:
        self.cycles_out_path = Path(cycles_out)
        self.gate_log_path = Path(gate_log) if gate_log is not None else None
        self.project_dir = Path(project_dir) if project_dir is not None else None
        # The Sentinel needs a project dir for its driver path; when the caller
        # does not supply one, fall back to the cycles.out parent (read-only).
        sentinel_project_dir = (
            self.project_dir
            if self.project_dir is not None
            else self.cycles_out_path.parent
        )
        self.sentinel = Sentinel(
            project_dir=sentinel_project_dir,
            cycles_out=self.cycles_out_path,
            gate_log=self.gate_log_path,
        )

    def summary(self) -> IntegrationSummary:
        """Aggregate a read-only per-cycle summary from the watched paths."""
        started, done = self.sentinel.parse_cycles()
        in_flight = self.sentinel.get_in_flight_cycles()
        gate_blocks = self.sentinel.gate_block_cycles()
        wall_kill_candidates = [c for c in in_flight if c not in gate_blocks]
        first_not_done = in_flight[0] if in_flight else None
        return IntegrationSummary(
            started=started,
            done=done,
            in_flight=in_flight,
            wall_kill_candidates=wall_kill_candidates,
            gate_blocks=gate_blocks,
            first_not_done=first_not_done,
        )
