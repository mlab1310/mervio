"""Travaux de fond: worker, gestionnaires, retention (Mission 004.2).

Le paquet n'importe aucun pilote de base: il passe par `mervio.persistence`
(verifie par `tests/test_persistence_boundaries.py`). Importer ce paquet exige
l'extra `persistence`.
"""
from .errors import JobExecutionError, PermanentJobError, RetryableJobError, classify
from .handlers import HandlerRegistry, JobContext, default_registry
from .lease import LeaseKeeper
from .lifecycle import IllegalTransition, Lifecycle, ProcessState
from .retention import RetentionPolicy
from .worker import JobOutcome, Worker, cancel, default_worker_id, enqueue

__all__ = [
    "HandlerRegistry", "IllegalTransition", "JobContext", "JobExecutionError", "JobOutcome", "LeaseKeeper",
    "Lifecycle", "PermanentJobError", "ProcessState", "RetentionPolicy", "RetryableJobError", "Worker", "cancel",
    "classify", "default_registry", "default_worker_id", "enqueue",
]
