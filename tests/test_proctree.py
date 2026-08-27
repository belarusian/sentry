"""Tests for sentry.proctree.ProcessTree (TICKET-033 / TICKET-039).

Uses ``patch.object(instance, 'method')`` throughout — never constructor-level
patches. No ambient ``/proc`` state: the ``_scan_processes`` / ``_ppid_map`` /
``_read_cmdline`` seams are always patched.
"""

from __future__ import annotations

from unittest.mock import patch

from sentry.proctree import ProcessTree


def _tree() -> ProcessTree:
    return ProcessTree()


# -- descendants: depth-correct walk ----------------------------------------


def test_descendants_includes_grandchild_excludes_sibling() -> None:
    """A grandchild of the root is included; a sibling (not a descendant) is
    not."""
    tree = _tree()
    # root=100; child=200 (ppid 100); grandchild=300 (ppid 200); sibling=400
    # (ppid 100's sibling, ppid 999 -> not under 100).
    procs = [
        (100, "python3 run.py"),
        (200, "bash -c 'python3 cycle-implementation.py'"),
        (300, "python3 cycle-implementation.py --cycle 9"),
        (400, "bash -c 'unrelated'"),
    ]
    ppid_map = {200: 100, 300: 200, 400: 999}
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        result = tree.descendants(100)
    pids = {pid for pid, _ in result}
    assert 200 in pids
    assert 300 in pids
    assert 400 not in pids
    assert 100 not in pids  # the root itself is not a strict descendant


def test_descendants_returns_cmdlines() -> None:
    tree = _tree()
    procs = [
        (100, "python3 run.py"),
        (200, "bash -c 'validator'"),
    ]
    ppid_map = {200: 100}
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        result = tree.descendants(100)
    assert result == [(200, "bash -c 'validator'")]


def test_descendants_empty_when_no_descendants() -> None:
    tree = _tree()
    procs = [(100, "python3 run.py")]
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value={}),
    ):
        assert tree.descendants(100) == []


def test_descendants_cycle_guarded() -> None:
    """A malformed ppid_map with a cycle must not loop forever."""
    tree = _tree()
    procs = [(100, "python3 run.py"), (200, "bash"), (300, "bash")]
    # 200 -> 300 -> 200 (cycle), neither reaches 100.
    ppid_map = {200: 300, 300: 200}
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        assert tree.descendants(100) == []


# -- has_live_work: non-LLM child signal ------------------------------------


def test_has_live_work_true_when_bash_child_exists() -> None:
    """TICKET-033: a live bash child under the root is live work."""
    tree = _tree()
    procs = [
        (100, "python3 run.py"),
        (200, "bash -c 'python3 auditor.py'"),
    ]
    ppid_map = {200: 100}
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        is_live, samples = tree.has_live_work(100)
    assert is_live is True
    assert samples == ["bash -c 'python3 auditor.py'"]


def test_has_live_work_false_for_bare_tree() -> None:
    """TICKET-033: a root with no descendants is not live work."""
    tree = _tree()
    procs = [(100, "python3 run.py")]
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value={}),
    ):
        is_live, samples = tree.has_live_work(100)
    assert is_live is False
    assert samples == []


def test_has_live_work_false_when_only_llm_machinery() -> None:
    """A root whose only descendants are the LLM machinery (run.py / the
    spoke) is NOT live work — that is the LLM, not work it is waiting on."""
    tree = _tree()
    procs = [
        (100, "python3 run.py"),
        (200, "python3 cycle-implementation.py --cycle 9"),
    ]
    ppid_map = {200: 100}
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        is_live, samples = tree.has_live_work(100)
    assert is_live is False
    assert samples == []


def test_has_live_work_false_when_only_driver() -> None:
    """The driver (run-cycles / run*.py) is never live work."""
    tree = _tree()
    procs = [
        (100, "python3 run.py"),
        (200, "bash run-cycles-v1.sh 1 3"),
    ]
    ppid_map = {200: 100}
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        is_live, samples = tree.has_live_work(100)
    assert is_live is False
    assert samples == []


def test_has_live_work_true_for_gh_npm_pytest_sleep() -> None:
    """Each work-process class (gh/npm/pytest/sleep) counts as live work."""
    tree = _tree()
    for cmdline in (
        "gh pr list --repo x/y",
        "npm install",
        "python -m pytest tests/",
        "sleep 300",
    ):
        procs = [(100, "python3 run.py"), (200, cmdline)]
        ppid_map = {200: 100}
        with (
            patch.object(tree, "_scan_processes", return_value=procs),
            patch.object(tree, "_ppid_map", return_value=ppid_map),
        ):
            is_live, samples = tree.has_live_work(100)
        assert is_live is True, cmdline
        assert samples == [cmdline]


def test_has_live_work_caps_samples() -> None:
    """sample_cmdlines is capped (short list for the log payload)."""
    tree = _tree()
    procs = [(100, "python3 run.py")]
    ppid_map: dict[int, int] = {}
    for i in range(1, 8):
        procs.append((100 + i, f"bash -c 'work {i}'"))
        ppid_map[100 + i] = 100
    with (
        patch.object(tree, "_scan_processes", return_value=procs),
        patch.object(tree, "_ppid_map", return_value=ppid_map),
    ):
        is_live, samples = tree.has_live_work(100)
    assert is_live is True
    assert len(samples) == 5


# -- constructor: no I/O ----------------------------------------------------


def test_init_stores_state_only() -> None:
    """__init__ stores the root pid and marker; no I/O (no /proc read)."""
    tree = ProcessTree(root_pid=42, marker="cycle-implementation.py")
    assert tree.root_pid == 42
    assert tree.marker == "cycle-implementation.py"


def test_init_defaults() -> None:
    tree = ProcessTree()
    assert tree.root_pid is None
    assert tree.marker is None


# -- stdlib-only invariant --------------------------------------------------


def test_module_is_stdlib_only() -> None:
    """sentry.proctree imports only stdlib modules (no third-party)."""
    import sys

    import sentry.proctree as mod

    # Every module object referenced by the proctree namespace must be stdlib
    # (or the package itself).
    for name in dir(mod):
        obj = getattr(mod, name)
        if not hasattr(obj, "__spec__") or obj.__spec__ is None:
            continue
        top = (obj.__name__ or "").split(".")[0]
        if top in ("builtins", "types", "typing", "sentry"):
            continue
        assert top in sys.stdlib_module_names, f"non-stdlib import: {top} ({name})"
