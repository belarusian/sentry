"""Tests for sentry.sentinel (Cycle 1 Sentinel Core)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sentry.sentinel import DetectionResult, Sentinel


def _write_cycles(tmp_path: Path, text: str) -> Path:
    out = tmp_path / "cycles.out"
    out.write_text(text, encoding="utf-8")
    return out


START_1 = "========== CYCLE 1  21:02:33Z ==========\n"
DONE_1 = "========== CYCLE 1 done ==========\n"
HEADER = "# endpoint: standard (.157:8080 fast-qwen / .161:8081 qwen)  21:02:33Z\n"


def test_parse_start_and_done(tmp_path):
    _write_cycles(
        tmp_path,
        HEADER + START_1 + "some noise\n" + DONE_1 + "========== CYCLE 2  21:03:00Z ==========\n",
    )
    sent = Sentinel(tmp_path)
    assert sent.get_in_flight_cycles() == [2]
    assert sent.get_first_not_done_cycle() == 2


def test_parse_ignores_header_and_noise(tmp_path):
    _write_cycles(
        tmp_path,
        HEADER + "========== CYCLE 1  21:02:33Z ==========\n" + DONE_1,
    )
    sent = Sentinel(tmp_path)
    assert sent.get_in_flight_cycles() == []
    assert sent.get_first_not_done_cycle() is None


def test_missing_cycles_out(tmp_path):
    sent = Sentinel(tmp_path)
    assert sent.get_in_flight_cycles() == []
    assert sent.get_first_not_done_cycle() is None


def test_two_space_start_vs_one_space_done(tmp_path):
    # start has two spaces before date; done has one space before 'done'.
    _write_cycles(
        tmp_path,
        "========== CYCLE 10  21:02:33Z ==========\n"
        "========== CYCLE 10 done ==========\n"
        "========== CYCLE 11  21:02:33Z ==========\n",
    )
    sent = Sentinel(tmp_path)
    assert sent.get_in_flight_cycles() == [11]


def test_detect_driver_death_detected(tmp_path):
    _write_cycles(tmp_path, START_1)
    sent = Sentinel(tmp_path)
    with patch.object(sent, "_scan_driver_processes", return_value=[]):
        result = sent.detect_driver_death()
    assert result.detected is True
    assert result.cycle == 1
    assert isinstance(result, DetectionResult)


def test_detect_driver_death_not_when_alive(tmp_path):
    _write_cycles(tmp_path, START_1)
    sent = Sentinel(tmp_path)
    with patch.object(
        sent, "_scan_driver_processes", return_value=[(123, "bash run-cycles.sh")]
    ):
        result = sent.detect_driver_death()
    assert result.detected is False


def test_detect_driver_death_not_when_no_in_flight(tmp_path):
    _write_cycles(tmp_path, START_1 + DONE_1)
    sent = Sentinel(tmp_path)
    with patch.object(sent, "_scan_driver_processes", return_value=[]):
        result = sent.detect_driver_death()
    assert result.detected is False
    assert result.reason == "no cycle in flight"


def test_detect_wall_kill_no_merge_detected(tmp_path):
    _write_cycles(tmp_path, START_1)
    gate = tmp_path / "gate.md"
    gate.write_text("## Cycle 1 — Pending\n", encoding="utf-8")
    sent = Sentinel(tmp_path, gate_log=gate)
    with patch.object(sent, "_scan_driver_processes", return_value=[]):
        result = sent.detect_wall_kill_no_merge()
    assert result.detected is True
    assert result.cycle == 1


def test_detect_wall_kill_no_merge_skipped_when_merged(tmp_path):
    _write_cycles(tmp_path, START_1)
    gate = tmp_path / "gate.md"
    gate.write_text("## Cycle 1: Sentinel Core\n**Status:** merged\n", encoding="utf-8")
    sent = Sentinel(tmp_path, gate_log=gate)
    with patch.object(sent, "_scan_driver_processes", return_value=[]):
        result = sent.detect_wall_kill_no_merge()
    assert result.detected is False


def test_detect_wall_kill_no_merge_skipped_when_alive(tmp_path):
    _write_cycles(tmp_path, START_1)
    gate = tmp_path / "gate.md"
    gate.write_text("## Cycle 1 — Pending\n", encoding="utf-8")
    sent = Sentinel(tmp_path, gate_log=gate)
    with patch.object(
        sent, "_scan_driver_processes", return_value=[(9, "python3 run.py")]
    ):
        result = sent.detect_wall_kill_no_merge()
    assert result.detected is False


def test_has_gate_block_pending_vs_merged(tmp_path):
    gate = tmp_path / "gate.md"
    gate.write_text("## Cycle 1 — Pending\n", encoding="utf-8")
    sent = Sentinel(tmp_path, gate_log=gate)
    assert sent.has_gate_block(1) is False
    gate.write_text("## Cycle 1: Sentinel Core\n## Cycle 2 — Pending\n", encoding="utf-8")
    assert sent.has_gate_block(1) is True
    assert sent.has_gate_block(2) is False


def test_is_driver_process_alive_true(tmp_path):
    sent = Sentinel(tmp_path)
    with patch.object(
        sent, "_scan_driver_processes", return_value=[(5, "bash run-cycles.sh")]
    ):
        assert sent.is_driver_process_alive() is True


def test_is_driver_process_alive_false(tmp_path):
    sent = Sentinel(tmp_path)
    with patch.object(sent, "_scan_driver_processes", return_value=[]):
        assert sent.is_driver_process_alive() is False


def test_matches_driver_patterns(tmp_path):
    sent = Sentinel(tmp_path)
    assert sent._matches_driver("bash run-cycles.sh --start-cycle 1") is True
    assert sent._matches_driver("python3 /x/run.py --goal g") is True
    assert sent._matches_driver("python3 -m pytest tests/") is False
    assert sent._matches_driver("bash run-setup-v3.sh") is False


def test_detection_result_to_dict(tmp_path):
    r = DetectionResult(True, cycle=3, reason="x", evidence={"a": 1})
    assert r.to_dict() == {"detected": True, "cycle": 3, "reason": "x", "evidence": {"a": 1}}


@pytest.fixture
def _unused():
    yield
