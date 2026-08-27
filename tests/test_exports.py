"""Tests for sentry package public exports (Cycle 1 Sentinel Core)."""

from __future__ import annotations

import sentry


def test_exports_present():
    assert sentry.Sentinel is not None
    assert sentry.DetectionResult is not None
    assert sentry.Relauncher is not None
    assert sentry.RelaunchResult is not None
    assert sentry.SentryLog is not None
    assert sentry.EndpointProbe is not None
    assert sentry.ProcessTree is not None


def test_all_names_resolvable():
    for name in sentry.__all__:
        assert hasattr(sentry, name), f"missing export: {name}"


def test_version():
    assert sentry.__version__
