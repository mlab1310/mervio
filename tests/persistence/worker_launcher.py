"""Lanceur du processus worker pour les tests de signaux reels (Mission 004.3).

    PYTHONPATH=tests:src python -m persistence.worker_launcher

Depuis 004.3.6, il passe par le point d'entree de `mervio worker` (`cli.worker.run_worker`):
meme lecture de configuration, memes logs, memes signaux, memes codes de sortie. Il n'y
ajoute que le gestionnaire pilote des tests et les variables `MERVIO_TEST_*` (baux courts,
que la configuration de production refuse volontairement).
"""
from __future__ import annotations

import dataclasses
import os
import sys

from mervio.cli.worker import run_worker
from mervio.settings import WorkerSettings

from persistence.worker_support import scripted_registry

OVERRIDES = {
    "MERVIO_TEST_LEASE_SECONDS": ("lease_seconds", int),
    "MERVIO_TEST_RENEW_SECONDS": ("lease_renew_seconds", float),
}


def load_settings() -> WorkerSettings:
    settings = WorkerSettings.from_env()
    changes = {name: kind(os.environ[variable]) for variable, (name, kind) in OVERRIDES.items()
               if variable in os.environ}
    return dataclasses.replace(settings, **changes)


def main() -> int:
    return run_worker(settings_loader=load_settings, registry=scripted_registry(),
                      forced_exit_delay=float(os.environ.get("MERVIO_TEST_FORCED_EXIT_DELAY", "5")))


if __name__ == "__main__":
    sys.exit(main())
