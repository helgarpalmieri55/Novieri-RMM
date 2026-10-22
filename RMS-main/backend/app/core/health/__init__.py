"""Platform health: probes for the control plane's own dependencies.

NOT `app/modules/health/`, which is DEVICE health (endpoint monitoring, alert
profiles, remediation). This is kernel infrastructure -- it reports on the
kernel every module sits on, so it does not register through
`modules.register_all`.
"""

from app.core.health.registry import (
    CACHE_TTL_S,
    PROBE_TIMEOUT_S,
    TOTAL_BUDGET_S,
    ProbeRegistry,
)
from app.core.health.types import Probe, ProbeResult, ReasonCode, Status

__all__ = [
    "CACHE_TTL_S",
    "PROBE_TIMEOUT_S",
    "TOTAL_BUDGET_S",
    "Probe",
    "ProbeRegistry",
    "ProbeResult",
    "ReasonCode",
    "Status",
]
