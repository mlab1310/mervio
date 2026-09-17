"""Lanceur du processus worker pour les tests de signaux reels (Mission 004.3).

    PYTHONPATH=tests:src python -m persistence.worker_launcher

Ce n'est pas la commande `mervio worker` (004.3.6): c'est le strict necessaire pour
envoyer de vrais SIGTERM/SIGINT/SIGKILL a un vrai processus. La configuration vient de
l'environnement `MERVIO_*`; les variables `MERVIO_TEST_*` n'existent que pour les tests
(baux courts, que la configuration de production refuse volontairement).
"""
from __future__ import annotations

import dataclasses
import os
import sys

from mervio.settings import WorkerSettings
from mervio.workers.runtime import WorkerRuntime, configure_process_logging

from persistence.worker_support import scripted_registry

OVERRIDES = {
    "MERVIO_TEST_LEASE_SECONDS": ("lease_seconds", int),
    "MERVIO_TEST_RENEW_SECONDS": ("lease_renew_seconds", float),
}


def main() -> int:
    settings = WorkerSettings.from_env()
    changes = {name: kind(os.environ[variable]) for variable, (name, kind) in OVERRIDES.items()
               if variable in os.environ}
    settings = dataclasses.replace(settings, **changes)
    configure_process_logging(settings, stream=sys.stderr)
    runtime = WorkerRuntime(settings, registry=scripted_registry(),
                            forced_exit_delay=float(os.environ.get("MERVIO_TEST_FORCED_EXIT_DELAY", "5")))
    runtime.install_signal_handlers()
    return runtime.run().exit_code


if __name__ == "__main__":
    sys.exit(main())
