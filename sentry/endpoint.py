"""EndpointProbe: read llama.cpp inference-endpoint state via ``/metrics``.

TICKET-032 / TICKET-034 / TICKET-035. The stall decision (TICKET-036) needs
to distinguish a *healthy-slow* inference (a request is in flight AND the
generation counters are advancing) from a *wedged* one. Both llama.cpp servers
expose a Prometheus ``/metrics`` endpoint with the four series of interest::

    llamacpp:requests_processing         gauge   requests in flight right now
    llamacpp:tokens_predicted_total      counter generation tokens processed
    llamacpp:n_decode_total              counter llama_decode() calls
    llamacpp:predicted_tokens_seconds    gauge   average generation throughput t/s

``/health`` returns ``{"status":"ok"}`` (liveness only — no inference state).

This module is stdlib-only (``urllib``). The HTTP fetch is isolated in a small
overridable :meth:`EndpointProbe._fetch` so tests can ``patch.object`` it
without ambient network state.
"""

from __future__ import annotations

import urllib.error
import urllib.request

# The four llama.cpp series of interest (TICKET-034).
_METRIC_NAMES = (
    "llamacpp:requests_processing",
    "llamacpp:tokens_predicted_total",
    "llamacpp:n_decode_total",
    "llamacpp:predicted_tokens_seconds",
)

# The generation counters used by :meth:`EndpointProbe.generating` (TICKET-035).
_COUNTER_NAMES = ("tokens_predicted_total", "n_decode_total")


def _parse_prometheus(text: str) -> dict[str, float]:
    """Parse Prometheus text exposition into ``{series_name: last_value}``.

    For each of the four ``llamacpp:*`` series, take the *last* sample value.
    ``# TYPE`` / ``# HELP`` comment lines are ignored, and label suffixes
    (``name{...} value``) are stripped so only the bare series name is kept.
    """
    values: dict[str, float] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # A sample line is ``name{labels} value`` or ``name value``.
        head = stripped.split(" ", 1)[0]
        name = head
        if "{" in name:
            name = name.split("{", 1)[0]
        if name not in _METRIC_NAMES:
            continue
        parts = stripped.split()
        if len(parts) < 2:
            continue
        try:
            values[name] = float(parts[-1])
        except ValueError:
            continue
    return values


class EndpointProbe:
    """Probe llama.cpp inference endpoints for live inference state.

    No module-level state and no I/O in ``__init__``: the constructor only
    stores the base URLs and timeout. All network access happens inside
    :meth:`probe` (via the overridable :meth:`_fetch`).
    """

    def __init__(self, base_urls: tuple[str, ...], timeout: float = 5.0) -> None:
        self.base_urls = tuple(base_urls)
        self.timeout = timeout

    # -- HTTP seam (overridable in tests) -----------------------------------

    def _fetch(self, url: str) -> tuple[int, str]:
        """GET ``url`` and return ``(status_code, body)``.

        Uses ``urllib.request.urlopen`` with the configured timeout. Raises
        ``urllib.error.URLError`` / ``OSError`` on timeout, connection
        refused, or any transport failure.
        """
        request = urllib.request.Request(url)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read().decode("utf-8", "replace")
        return int(status), body

    # -- probe --------------------------------------------------------------

    def _probe_one(self, base: str) -> dict[str, object]:
        """Probe a single base URL and return its per-endpoint dict."""
        base = base.rstrip("/")
        metrics_url = f"{base}/metrics"
        try:
            status, body = self._fetch(metrics_url)
        except (urllib.error.URLError, OSError):
            # Timeout / connection refused / any transport failure: blind,
            # NOT wedged (TICKET-034).
            return self._blind()
        if status == 501 or not body.strip():
            # /metrics unsupported or empty: fall back to /health (liveness
            # only — the endpoint is up but blind to inference state).
            return self._health_fallback(base)
        if status != 200:
            # Non-200 (other than the 501 handled above): blind.
            return self._blind()
        parsed = _parse_prometheus(body)
        return {
            "reachable": True,
            "requests_processing": parsed.get("llamacpp:requests_processing"),
            "tokens_predicted_total": parsed.get("llamacpp:tokens_predicted_total"),
            "n_decode_total": parsed.get("llamacpp:n_decode_total"),
            "predicted_tokens_seconds": parsed.get("llamacpp:predicted_tokens_seconds"),
        }

    def _blind(self) -> dict[str, object]:
        """A blind per-endpoint dict: unreachable, all fields None."""
        return {
            "reachable": False,
            "requests_processing": None,
            "tokens_predicted_total": None,
            "n_decode_total": None,
            "predicted_tokens_seconds": None,
        }

    def _health_fallback(self, base: str) -> dict[str, object]:
        """Fall back to ``GET <base>/health``: reachable if it answers 200."""
        health_url = f"{base}/health"
        try:
            status, _body = self._fetch(health_url)
        except (urllib.error.URLError, OSError):
            return self._blind()
        if status != 200:
            return self._blind()
        return {
            "reachable": True,
            "requests_processing": None,
            "tokens_predicted_total": None,
            "n_decode_total": None,
            "predicted_tokens_seconds": None,
        }

    def probe(self) -> dict[str, dict[str, object]]:
        """Probe every base URL.

        Returns a dict keyed by base URL; each value is
        ``{reachable, requests_processing, tokens_predicted_total,
        n_decode_total, predicted_tokens_seconds}``.
        """
        return {base: self._probe_one(base) for base in self.base_urls}

    # -- two-sample delta (TICKET-035) --------------------------------------

    def generating(self, samples: tuple[dict[str, dict[str, object]], ...]) -> bool:
        """True when a generation counter strictly increased between samples.

        ``samples`` is two (or more) :meth:`probe` results taken at an
        interval. Returns True when, for ANY endpoint, a generation counter
        (``tokens_predicted_total`` or ``n_decode_total``) strictly increased
        from the first sample to the last. Returns False when fewer than two
        samples are supplied, the counter is ``None`` in either sample (blind /
        not reported), or the counter is flat or decreased (a decrease means a
        server restart, not generation). Pure over its input — no I/O, no
        sleep.
        """
        if len(samples) < 2:
            return False
        first = samples[0]
        last = samples[-1]
        for base in first:
            first_entry = first.get(base)
            last_entry = last.get(base)
            if not isinstance(first_entry, dict) or not isinstance(last_entry, dict):
                continue
            for counter in _COUNTER_NAMES:
                first_val = first_entry.get(counter)
                last_val = last_entry.get(counter)
                if not isinstance(first_val, float) or not isinstance(last_val, float):
                    continue
                if last_val > first_val:
                    return True
        return False
