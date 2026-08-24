"""Integration tests against the REAL read-only project artifacts (Cycle 3).

Reads the actual ``cycles.out`` + gate logs for launch-gate (standard
``CYCLE N`` dialect) and fourseer (prefixed ``FOURSEER CYCLE N`` dialect) and
asserts the Integrator/Sentinel report the expected cycle counts and
wall-kill candidates. Skips gracefully when an artifact is absent
(portability). Also pins BOTH marker dialects with a synthetic fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sentry.integration import IntegrationSummary, Integrator
from sentry.sentinel import Sentinel

LAUNCH_GATE_DIR = Path("/home/sasha/AI/launch-gate")
LAUNCH_GATE_CYCLES = LAUNCH_GATE_DIR / "cycles.out"
LAUNCH_GATE_GATE = LAUNCH_GATE_DIR / "ai" / "cycle-001-launch-gate-gate.md"

FOURSEER_DIR = Path("/home/sasha/AI/fourseer")
FOURSEER_CYCLES = FOURSEER_DIR / "cycles.out"
FOURSEER_GATE = FOURSEER_DIR / "ai" / "cycle-001-fourseer-gate.md"


def _require(*paths: Path) -> None:
    """Skip the test when any required real artifact is absent."""
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        pytest.skip(f"real artifact(s) absent: {missing}")


def _snapshot(root: Path) -> set[str]:
    """Absolute file paths under ``root`` (for read-only assertions)."""
    return {os.path.join(r, f) for r, _dirs, files in os.walk(root) for f in files}


# -- real artifacts ---------------------------------------------------------


def test_launch_gate_real_artifacts():
    _require(LAUNCH_GATE_CYCLES, LAUNCH_GATE_GATE)
    integ = Integrator(
        LAUNCH_GATE_CYCLES, gate_log=LAUNCH_GATE_GATE, project_dir=LAUNCH_GATE_DIR
    )
    summary = integ.summary()
    assert isinstance(summary, IntegrationSummary)
    # 13 cycles, all done (standard `CYCLE N` dialect).
    assert summary.started == set(range(1, 14))
    assert summary.done == set(range(1, 14))
    assert summary.in_flight == []
    assert summary.wall_kill_candidates == []
    assert summary.first_not_done is None
    # Cycle 2 was wall-killed (SIGALRM, no saved trajectory) and has no gate
    # block; the other cycles do.
    assert 2 not in summary.gate_blocks
    assert summary.gate_blocks >= {1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13}


def test_fourseer_real_artifacts():
    _require(FOURSEER_CYCLES, FOURSEER_GATE)
    integ = Integrator(
        FOURSEER_CYCLES, gate_log=FOURSEER_GATE, project_dir=FOURSEER_DIR
    )
    summary = integ.summary()
    assert isinstance(summary, IntegrationSummary)
    # Cycles 7-13, all done (prefixed `FOURSEER CYCLE N` dialect; non-empty).
    assert summary.started == set(range(7, 14))
    assert summary.done == set(range(7, 14))
    assert summary.started  # non-empty — the dialect actually parsed
    assert summary.in_flight == []
    assert summary.wall_kill_candidates == []
    assert summary.first_not_done is None


def test_integrator_is_read_only():
    _require(LAUNCH_GATE_CYCLES, LAUNCH_GATE_GATE)
    before = _snapshot(LAUNCH_GATE_DIR)
    integ = Integrator(
        LAUNCH_GATE_CYCLES, gate_log=LAUNCH_GATE_GATE, project_dir=LAUNCH_GATE_DIR
    )
    integ.summary()
    after = _snapshot(LAUNCH_GATE_DIR)
    assert before == after


def test_sentinel_real_fourseer_in_flight_empty():
    _require(FOURSEER_CYCLES)
    sent = Sentinel(FOURSEER_DIR, cycles_out=FOURSEER_CYCLES)
    assert sent.get_in_flight_cycles() == []
    assert sent.get_first_not_done_cycle() is None


# -- marker dialect pinning (synthetic, always runs) ------------------------


def test_both_marker_dialects_parse(tmp_path):
    text = (
        "========== CYCLE 1  21:02:33Z ==========\n"
        "========== CYCLE 1 done ==========\n"
        "========== FOURSEER CYCLE 7  21:39:53Z ==========\n"
        "========== FOURSEER CYCLE 7 done ==========\n"
        "========== FOURSEER CYCLE 8  22:34:15Z ==========\n"
    )
    out = tmp_path / "cycles.out"
    out.write_text(text, encoding="utf-8")
    sent = Sentinel(tmp_path)
    started, done = sent.parse_cycles()
    # Standard `CYCLE N` and prefixed `FOURSEER CYCLE N` both parse.
    assert started == {1, 7, 8}
    assert done == {1, 7}
    assert sent.get_in_flight_cycles() == [8]
    assert sent.get_first_not_done_cycle() == 8


def test_standard_dialect_regression_safe(tmp_path):
    # The bare `CYCLE N` grammar must keep working unchanged.
    text = (
        "========== CYCLE 10  21:02:33Z ==========\n"
        "========== CYCLE 10 done ==========\n"
        "========== CYCLE 11  21:02:33Z ==========\n"
    )
    out = tmp_path / "cycles.out"
    out.write_text(text, encoding="utf-8")
    sent = Sentinel(tmp_path)
    started, done = sent.parse_cycles()
    assert started == {10, 11}
    assert done == {10}
    assert sent.get_in_flight_cycles() == [11]
