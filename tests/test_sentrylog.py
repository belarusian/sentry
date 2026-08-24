"""Tests for sentry.sentrylog (Cycle 1 Sentinel Core)."""

from __future__ import annotations

import threading
from pathlib import Path

from sentry.sentrylog import LogEntry, SentryLog


def test_append_returns_position_in_order(tmp_path):
    log = SentryLog(tmp_path / "log.jsonl")
    assert log.append("a", {"x": 1}) == 0
    assert log.append("b", {"y": 2}) == 1
    assert log.append("c", {}) == 2
    assert log.size() == 3


def test_read_all_in_order(tmp_path):
    log = SentryLog(tmp_path / "log.jsonl")
    log.append("a", {"x": 1})
    log.append("b", {"y": 2})
    entries = log.read_all()
    assert [e.position for e in entries] == [0, 1]
    assert [e.event_type for e in entries] == ["a", "b"]
    assert entries[0].payload == {"x": 1}
    assert entries[1].payload == {"y": 2}
    assert all(isinstance(e, LogEntry) for e in entries)


def test_read_since(tmp_path):
    log = SentryLog(tmp_path / "log.jsonl")
    for i in range(5):
        log.append("e", {"i": i})
    since = log.read_since(3)
    assert [e.position for e in since] == [3, 4]
    assert log.read_since(0) == log.read_all()
    assert log.read_since(99) == []


def test_size_empty(tmp_path):
    log = SentryLog(tmp_path / "log.jsonl")
    assert log.size() == 0
    assert log.read_all() == []
    assert log.read_since(0) == []


def test_append_only_preserves_order_across_instances(tmp_path):
    path = tmp_path / "log.jsonl"
    a = SentryLog(path)
    a.append("a", {"n": 0})
    a.append("a", {"n": 1})
    # A second writer instance appends; order is by position, not instance.
    b = SentryLog(path)
    assert b.append("b", {"n": 2}) == 2
    entries = b.read_all()
    assert [e.event_type for e in entries] == ["a", "a", "b"]
    assert [e.position for e in entries] == [0, 1, 2]


def test_single_writer_concurrent_appends_no_loss(tmp_path):
    path = tmp_path / "log.jsonl"
    log = SentryLog(path)
    n_threads, per = 8, 25

    def worker():
        for _ in range(per):
            log.append("w", {})

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert log.size() == n_threads * per
    # positions are a contiguous 0..N-1 sequence (order preserved)
    assert [e.position for e in log.read_all()] == list(range(n_threads * per))


def test_log_entry_to_dict(tmp_path):
    log = SentryLog(tmp_path / "log.jsonl")
    log.append("relaunch", {"cycle": 1})
    e = log.read_all()[0]
    d = e.to_dict()
    assert d["position"] == 0
    assert d["event_type"] == "relaunch"
    assert d["payload"] == {"cycle": 1}
    assert d["ts"] is not None
