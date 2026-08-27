"""Tests for sentry.endpoint.EndpointProbe (TICKET-034 / 035 / 038).

Uses ``patch.object(instance, '_fetch')`` for ALL I/O — no ambient network
state. Writable paths come from ``tmp_path``.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path
from unittest.mock import patch

from sentry.endpoint import EndpointProbe

# A real /metrics fixture with the four llamacpp:* series of interest,
# including comment lines and label suffixes that must be ignored.
METRICS_FIXTURE = """# HELP llamacpp:requests_processing Requests currently being processed
# TYPE llamacpp:requests_processing gauge
llamacpp:requests_processing 3
# HELP llamacpp:tokens_predicted_total Total number of tokens predicted
# TYPE llamacpp:tokens_predicted_total counter
llamacpp:tokens_predicted_total 12345.0
# HELP llamacpp:n_decode_total Total number of llama_decode calls
# TYPE llamacpp:n_decode_total counter
llamacpp:n_decode_total 6789.0
# HELP llamacpp:predicted_tokens_seconds Average generation throughput
# TYPE llamacpp:predicted_tokens_seconds gauge
llamacpp:predicted_tokens_seconds 17.5
# An unrelated series that must be ignored.
llamacpp:other_metric 999
# A labeled variant of a real series (label suffix must be stripped).
llamacpp:requests_processing{model="deep"} 4
"""


def _probe(base: str = "http://localhost:8080") -> EndpointProbe:
    return EndpointProbe((base,))


def _sample(**overrides) -> dict[str, object]:
    """Build a per-endpoint dict with sensible defaults + overrides."""
    base = {
        "reachable": True,
        "requests_processing": 0.0,
        "tokens_predicted_total": 0.0,
        "n_decode_total": 0.0,
        "predicted_tokens_seconds": 0.0,
    }
    base.update(overrides)
    return base


# -- parse: real /metrics fixture -------------------------------------------


def test_probe_parses_metrics_fixture(tmp_path: Path) -> None:
    probe = _probe()
    with patch.object(probe, "_fetch", return_value=(200, METRICS_FIXTURE)):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is True
    # The labeled variant (value 4) is the LAST sample for that series name.
    assert entry["requests_processing"] == 4.0
    assert entry["tokens_predicted_total"] == 12345.0
    assert entry["n_decode_total"] == 6789.0
    assert entry["predicted_tokens_seconds"] == 17.5


def test_probe_ignores_unrelated_series(tmp_path: Path) -> None:
    probe = _probe()
    body = "llamacpp:other_metric 999\nllamacpp:n_decode_total 42.0\n"
    with patch.object(probe, "_fetch", return_value=(200, body)):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["n_decode_total"] == 42.0
    # The unrelated series must not leak into any of the four fields.
    assert entry["tokens_predicted_total"] is None


def test_probe_missing_series_is_none(tmp_path: Path) -> None:
    probe = _probe()
    body = "llamacpp:requests_processing 1\n"
    with patch.object(probe, "_fetch", return_value=(200, body)):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["requests_processing"] == 1.0
    assert entry["tokens_predicted_total"] is None
    assert entry["n_decode_total"] is None
    assert entry["predicted_tokens_seconds"] is None


# -- probe failure handling -------------------------------------------------


def test_probe_timeout_is_blind(tmp_path: Path) -> None:
    probe = _probe()
    with patch.object(
        probe, "_fetch", side_effect=urllib.error.URLError("timed out")
    ):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is False
    assert entry["requests_processing"] is None
    assert entry["tokens_predicted_total"] is None
    assert entry["n_decode_total"] is None
    assert entry["predicted_tokens_seconds"] is None


def test_probe_connection_refused_is_blind(tmp_path: Path) -> None:
    probe = _probe()
    with patch.object(probe, "_fetch", side_effect=ConnectionRefusedError):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is False
    assert entry["requests_processing"] is None


def test_probe_non_200_is_blind(tmp_path: Path) -> None:
    probe = _probe()
    with patch.object(probe, "_fetch", return_value=(500, "boom")):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is False
    assert entry["requests_processing"] is None


def test_probe_501_falls_back_to_health(tmp_path: Path) -> None:
    """501 on /metrics -> GET /health; reachable True, fields None (blind)."""
    probe = _probe()
    calls: list[str] = []

    def fake_fetch(url: str) -> tuple[int, str]:
        calls.append(url)
        if url.endswith("/metrics"):
            return 501, "Not Implemented"
        return 200, '{"status":"ok"}'

    with patch.object(probe, "_fetch", side_effect=fake_fetch):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is True
    assert entry["requests_processing"] is None
    assert entry["tokens_predicted_total"] is None
    assert "http://localhost:8080/health" in calls


def test_probe_empty_metrics_falls_back_to_health(tmp_path: Path) -> None:
    """Empty /metrics body -> /health fallback: reachable True, fields None."""
    probe = _probe()

    def fake_fetch(url: str) -> tuple[int, str]:
        if url.endswith("/metrics"):
            return 200, "   \n"
        return 200, '{"status":"ok"}'

    with patch.object(probe, "_fetch", side_effect=fake_fetch):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is True
    assert entry["requests_processing"] is None


def test_probe_501_and_health_down_is_blind(tmp_path: Path) -> None:
    """501 on /metrics AND /health unreachable -> blind (reachable False)."""
    probe = _probe()

    def fake_fetch(url: str) -> tuple[int, str]:
        if url.endswith("/metrics"):
            return 501, "Not Implemented"
        raise urllib.error.URLError("refused")

    with patch.object(probe, "_fetch", side_effect=fake_fetch):
        result = probe.probe()
    entry = result["http://localhost:8080"]
    assert entry["reachable"] is False
    assert entry["requests_processing"] is None


def test_probe_multi_endpoint(tmp_path: Path) -> None:
    probe = EndpointProbe(("http://a:8080", "http://b:8081"))
    with patch.object(probe, "_fetch", return_value=(200, METRICS_FIXTURE)):
        result = probe.probe()
    assert set(result) == {"http://a:8080", "http://b:8081"}
    assert result["http://a:8080"]["n_decode_total"] == 6789.0
    assert result["http://b:8081"]["n_decode_total"] == 6789.0


# -- generating: two-sample counter delta (TICKET-035) ----------------------


def test_generating_true_when_counter_increases(tmp_path: Path) -> None:
    probe = _probe()
    base = "http://localhost:8080"
    samples = (
        {base: _sample(tokens_predicted_total=100.0)},
        {base: _sample(tokens_predicted_total=200.0)},
    )
    assert probe.generating(samples) is True


def test_generating_true_when_n_decode_increases(tmp_path: Path) -> None:
    probe = _probe()
    base = "http://localhost:8080"
    samples = (
        {base: _sample(n_decode_total=10.0)},
        {base: _sample(n_decode_total=11.0)},
    )
    assert probe.generating(samples) is True


def test_generating_false_when_flat(tmp_path: Path) -> None:
    probe = _probe()
    base = "http://localhost:8080"
    samples = (
        {base: _sample(tokens_predicted_total=100.0)},
        {base: _sample(tokens_predicted_total=100.0)},
    )
    assert probe.generating(samples) is False


def test_generating_false_when_decreased(tmp_path: Path) -> None:
    """A decrease means a server restart, not generation -> False."""
    probe = _probe()
    base = "http://localhost:8080"
    samples = (
        {base: _sample(tokens_predicted_total=200.0)},
        {base: _sample(tokens_predicted_total=100.0)},
    )
    assert probe.generating(samples) is False


def test_generating_false_when_counter_none(tmp_path: Path) -> None:
    """Counter None in either sample (blind) -> False."""
    probe = _probe()
    base = "http://localhost:8080"
    samples = (
        {base: _sample(tokens_predicted_total=None)},
        {base: _sample(tokens_predicted_total=100.0)},
    )
    assert probe.generating(samples) is False


def test_generating_false_when_fewer_than_two_samples(tmp_path: Path) -> None:
    probe = _probe()
    base = "http://localhost:8080"
    assert probe.generating(()) is False
    assert probe.generating(({base: _sample(tokens_predicted_total=100.0)},)) is False


def test_generating_multi_endpoint_any_generating(tmp_path: Path) -> None:
    """One endpoint generating, another flat -> True (ANY endpoint)."""
    probe = EndpointProbe(("http://a:8080", "http://b:8081"))
    samples = (
        {
            "http://a:8080": _sample(tokens_predicted_total=100.0),
            "http://b:8081": _sample(tokens_predicted_total=50.0),
        },
        {
            "http://a:8080": _sample(tokens_predicted_total=100.0),  # flat
            "http://b:8081": _sample(tokens_predicted_total=60.0),  # increased
        },
    )
    # b increased -> True (ANY endpoint generating wins over a flat one).
    assert probe.generating(samples) is True


def test_generating_uses_first_and_last_of_many(tmp_path: Path) -> None:
    """With >2 samples, compare first to last."""
    probe = _probe()
    base = "http://localhost:8080"
    samples = (
        {base: _sample(tokens_predicted_total=100.0)},
        {base: _sample(tokens_predicted_total=150.0)},
        {base: _sample(tokens_predicted_total=200.0)},
    )
    assert probe.generating(samples) is True


# -- decision consumption: the WAIT condition is derivable from the signal ---


def test_signal_yields_wait_condition_when_active_and_generating(tmp_path: Path) -> None:
    """TICKET-038: two probe() samples with requests_processing > 0 and an
    advancing counter yield the WAIT condition (rp_positive AND generating)."""
    probe = _probe()
    base = "http://localhost:8080"
    first = {base: _sample(requests_processing=1.0, tokens_predicted_total=100.0)}
    second = {base: _sample(requests_processing=1.0, tokens_predicted_total=150.0)}
    with patch.object(probe, "probe", side_effect=[first, second]):
        samples = (probe.probe(), probe.probe())
    rp_positive = any(
        e.get("requests_processing") is not None and e.get("requests_processing") > 0
        for e in samples[0].values()
    )
    generating = probe.generating(samples)
    assert rp_positive is True
    assert generating is True
    # The WAIT gate condition (TICKET-036/037).
    assert rp_positive and generating
