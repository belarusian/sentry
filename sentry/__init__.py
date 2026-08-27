"""sentry: a deterministic, stdlib-only Python supervisor for pipeline rescue."""

from sentry.endpoint import EndpointProbe
from sentry.integration import IntegrationSummary, Integrator
from sentry.proctree import ProcessTree
from sentry.relaunch import Relauncher, RelaunchResult
from sentry.sentinel import DetectionResult, Sentinel
from sentry.sentrylog import LogEntry, SentryLog
from sentry.stall import StallDecision, StallMonitor, StallResult

__version__ = "0.2.0"

__all__ = [
    "DetectionResult",
    "EndpointProbe",
    "IntegrationSummary",
    "Integrator",
    "LogEntry",
    "ProcessTree",
    "RelaunchResult",
    "Relauncher",
    "Sentinel",
    "SentryLog",
    "StallDecision",
    "StallMonitor",
    "StallResult",
    "__version__",
]
