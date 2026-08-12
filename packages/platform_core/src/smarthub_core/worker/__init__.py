"""Worker base class and standardized health endpoint."""

from smarthub_core.worker.base import (
    FatalError,
    Job,
    Result,
    RetryableError,
    SmarthubWorker,
    WorkerError,
)
from smarthub_core.worker.health import (
    HEALTH_DEGRADED,
    HEALTH_DOWN,
    HEALTH_OK,
    compute_health_report,
)

__all__ = [
    "SmarthubWorker",
    "Job",
    "Result",
    "WorkerError",
    "RetryableError",
    "FatalError",
    "compute_health_report",
    "HEALTH_OK",
    "HEALTH_DEGRADED",
    "HEALTH_DOWN",
]
