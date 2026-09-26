"""Controle de capacite de destruction au demarrage du worker (004.4.5, D-064). Sans base.

D-064 exige un controle FAIL-CLOSED: un worker qui sert `redact_customer` refuse de demarrer si
son magasin d'objets ne peut pas detruire. Le probleme qu'il ferme est precis -- sans lui, une
mauvaise configuration (typiquement un magasin monte en lecture seule) ne se manifeste qu'au
MILIEU d'un effacement: la base a deja fait passer les objets en `purging`, puis la destruction
echoue. Le refus doit donc arriver AVANT la moindre connexion, et c'est ce que ces tests verifient.

Aucune base n'est necessaire, et c'est le point: l'URL fournie ici est injoignable. Si un test
passait la phase de preflight, il echouerait sur la connexion avec un autre code (3), ce qui
distingue nettement les deux refus.

Le cas nominal "magasin capable -> le worker demarre" est couvert par
tests/persistence/test_worker_runtime.py, dont chaque runtime sert tous les types de travaux avec
une racine d'objets inscriptible.
"""
from __future__ import annotations

import io
import json
import logging
import os

import pytest

from mervio.observability.logging import configure
from mervio.settings import WorkerSettings
from mervio.storage import DELETE_CAPABLE, DELETE_INCAPABLE, DELETE_UNDETERMINED
from mervio.workers.runtime import EXIT_CONFIG, EXIT_DATABASE, WorkerRuntime

#: injoignable a dessein: la connexion n'est jamais censee etre tentee quand le preflight refuse
UNREACHABLE = "postgresql://svc@127.0.0.1:1/mervio"
MASTER_HEX = "7f" * 32


@pytest.fixture
def restore_logging():
    root = logging.getLogger("mervio")
    state = (list(root.handlers), root.propagate, root.level)
    yield
    root.handlers, root.propagate, root.level = state


def settings_for(tmp_path, *, job_types=None, root=None, **extra) -> WorkerSettings:
    variables = {
        "MERVIO_DATABASE_URL": UNREACHABLE,
        "MERVIO_WORKER_NAME": "preflight",
        "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX,
        "MERVIO_OBJECT_STORE_ROOT": str(root if root is not None else tmp_path / "objects"),
        "MERVIO_WORKER_STARTUP_TIMEOUT_SECONDS": "0",
        "MERVIO_WORKER_POLL_INTERVAL_SECONDS": "0.05",
        "MERVIO_WORKER_POLL_MAX_SECONDS": "0.2",
    }
    if job_types is not None:
        variables["MERVIO_WORKER_JOB_TYPES"] = job_types
    variables.update({key: str(value) for key, value in extra.items()})
    return WorkerSettings.from_env(variables)


def run(settings, *, stream=None):
    """Execute le runtime et rend (code de sortie, evenements JSON publies)."""
    stream = io.StringIO() if stream is None else stream
    configure(format="json", level=logging.DEBUG, service="worker", stream=stream)
    result = WorkerRuntime(settings, forced_exit=lambda code: None).run()
    events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip().startswith("{")]
    return result.exit_code, events


def find(events, name):
    return [event for event in events if event.get("event") == name]


def unwritable(path):
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o500)


# -- refus fail-closed --------------------------------------------------------------------------

def test_a_read_only_object_store_refuses_startup_with_the_configuration_code(tmp_path, restore_logging):
    """LE cas de D-064: racine non inscriptible, donc `unlink` impossible, donc refus au demarrage."""
    root = tmp_path / "objects"
    unwritable(root)
    try:
        code, events = run(settings_for(tmp_path, root=root))
    finally:
        os.chmod(root, 0o700)

    assert code == EXIT_CONFIG, "un magasin incapable de detruire doit etre refuse comme une configuration"
    assert code != EXIT_DATABASE, "le refus precede la connexion, il ne doit pas etre pris pour une panne"
    [refusal] = find(events, "worker.object_store_incapable")
    assert refusal["capability"] == DELETE_INCAPABLE
    assert refusal["job_type"] == "redact_customer"
    assert refusal["object_store"] == "filesystem"
    assert refusal["exit_code"] == EXIT_CONFIG


def test_the_refusal_names_only_the_rule_and_never_a_path(tmp_path, restore_logging):
    """Le message ne porte que la REGLE et le pilote: ni racine, ni chemin, ni valeur (D-016)."""
    root = tmp_path / "objects"
    unwritable(root)
    try:
        stream = io.StringIO()
        run(settings_for(tmp_path, root=root), stream=stream)
    finally:
        os.chmod(root, 0o700)
    published = stream.getvalue()
    assert str(root) not in published and str(tmp_path) not in published
    assert MASTER_HEX not in published
    [refusal] = find([json.loads(line) for line in published.splitlines() if line.strip().startswith("{")],
                     "worker.object_store_incapable")
    assert "detruire" in refusal["rule"] and "redact_customer" in refusal["rule"]
    assert "/" not in refusal["rule"]


def test_the_preflight_refuses_before_any_database_connection_is_attempted(tmp_path, restore_logging):
    """Preuve d'ordre: aucune tentative de connexion n'est publiee avant le refus."""
    root = tmp_path / "objects"
    unwritable(root)
    try:
        code, events = run(settings_for(tmp_path, root=root))
    finally:
        os.chmod(root, 0o700)
    assert code == EXIT_CONFIG
    assert find(events, "worker.database_connected") == []
    assert find(events, "worker.database_unavailable") == [], "la base n'a pas ete sollicitee"
    assert find(events, "worker.ready") == []


def test_the_preflight_performs_no_destructive_probe_on_the_object_tree(tmp_path, restore_logging):
    """Aucune sonde destructrice (D-064): l'arbre est identique avant et apres, octets compris."""
    from mervio.storage import FilesystemObjectStore, build_object_key
    import uuid

    root = tmp_path / "objects"
    store = FilesystemObjectStore(root)
    target = build_object_key(uuid.uuid4(), uuid.uuid4())
    store.put(target, io.BytesIO(b"octets-intacts"))
    snapshot = {p.relative_to(root).as_posix(): (p.is_file() and p.read_bytes())
                for p in sorted(root.rglob("*"))}

    code, _ = run(settings_for(tmp_path, root=root))

    assert code == EXIT_DATABASE, "magasin capable: le preflight passe et c'est la base qui manque"
    assert {p.relative_to(root).as_posix(): (p.is_file() and p.read_bytes())
            for p in sorted(root.rglob("*"))} == snapshot


# -- ce que le preflight ne fait PAS --------------------------------------------------------------

def test_a_worker_that_does_not_serve_the_erasure_is_not_checked_at_all(tmp_path, restore_logging):
    """Le controle est lie au TYPE DE TRAVAIL servi: un worker d'import n'a pas a pouvoir detruire."""
    root = tmp_path / "objects"
    unwritable(root)
    try:
        code, events = run(settings_for(tmp_path, root=root, job_types="import,analysis,purge"))
    finally:
        os.chmod(root, 0o700)
    assert find(events, "worker.object_store_incapable") == []
    assert code == EXIT_DATABASE, "il echoue sur la base, pas sur la capacite"


def test_a_capable_store_passes_the_preflight_and_reaches_the_database(tmp_path, restore_logging):
    code, events = run(settings_for(tmp_path))
    assert find(events, "worker.object_store_incapable") == []
    assert code == EXIT_DATABASE


def test_the_verdict_comes_from_the_store_itself_not_from_the_driver_name(tmp_path, restore_logging):
    """Le preflight interroge le magasin REEL; il ne deduit rien du nom du pilote.

    Un magasin qui se declare incapable est refuse meme si sa racine est parfaitement inscriptible:
    c'est ce qui garantit que le controle suit l'implementation et non une table de correspondance.
    """
    settings = settings_for(tmp_path)
    runtime = WorkerRuntime(settings, forced_exit=lambda code: None)
    assert runtime.object_store.delete_capability() == DELETE_CAPABLE

    class Incapable:
        def delete_capability(self):
            return DELETE_INCAPABLE

    runtime.object_store = Incapable()
    stream = io.StringIO()
    configure(format="json", level=logging.DEBUG, service="worker", stream=stream)
    assert runtime.run().exit_code == EXIT_CONFIG


# -- S3: `undetermined` est accepte, et journalise -----------------------------------------------

def test_an_undetermined_capability_is_accepted_and_explicitly_logged(tmp_path, restore_logging):
    """D-064 point 5: aucun controle non destructif ne prouve `s3:DeleteObject`.

    Le preflight ne refuse donc PAS sur `undetermined` -- ce serait interdire tout worker S3 --
    mais il le dit, avec la reference de la decision et le remede. C'est un aveu d'ignorance
    journalise, jamais un faux positif de capacite.
    """
    settings = settings_for(tmp_path)
    runtime = WorkerRuntime(settings, forced_exit=lambda code: None)

    class Undetermined:
        def delete_capability(self):
            return DELETE_UNDETERMINED

    runtime.object_store = Undetermined()
    stream = io.StringIO()
    configure(format="json", level=logging.DEBUG, service="worker", stream=stream)
    code = runtime.run().exit_code
    events = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip().startswith("{")]

    assert code == EXIT_DATABASE, "accepte: le worker continue et echoue seulement sur la base"
    assert find(events, "worker.object_store_incapable") == []
    [warned] = find(events, "worker.object_store_capability_undetermined")
    assert warned["capability"] == DELETE_UNDETERMINED
    assert warned["decision"] == "D-064"
    assert "s3:DeleteObject" in warned["remedy"] and "004.9" in warned["remedy"]
    assert warned["level"] == "WARNING"


def test_an_absent_object_store_is_refused_when_the_erasure_is_served(tmp_path, restore_logging):
    """Sans magasin du tout, un worker d'effacement ne peut rien detruire: meme refus."""
    settings = settings_for(tmp_path)
    runtime = WorkerRuntime(settings, forced_exit=lambda code: None)
    runtime.object_store = None
    stream = io.StringIO()
    configure(format="json", level=logging.DEBUG, service="worker", stream=stream)
    assert runtime.run().exit_code == EXIT_CONFIG
