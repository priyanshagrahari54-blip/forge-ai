"""Remote worker control-plane primitives."""

from forge.workers.admission import Admission, AdmissionError, WorkerAdmission, WorkerRequirements
from forge.workers.registry import WorkerRecord, WorkerRegistry

__all__ = [
    "Admission", "AdmissionError", "WorkerAdmission", "WorkerRequirements",
    "WorkerRecord", "WorkerRegistry",
]
