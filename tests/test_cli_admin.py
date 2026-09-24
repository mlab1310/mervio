"""`mervio admin` sans base de donnees (Mission 004.3.7): aide, arguments, codes, configuration, rendu.

Les operations contre PostgreSQL sont couvertes par tests/persistence/test_admin_operations.py et
le scenario de demonstration complet par tests/persistence/test_cli_admin_process.py.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from mervio.admin import errors as admin_errors
from mervio.admin import operations
from mervio.cli import admin as cli_admin
from mervio.cli import build_parser
from mervio.settings import AdminSettings, SettingsError

ROOT = Path(__file__).resolve().parent.parent
ORG = "3f0c7c1e-8a55-4d7e-9d0b-6a1f2e0b9c11"
STORE = "8d7c2a41-0c1a-4b7e-9a55-2f0e6b1d3c22"
SECRET = "Zq9-admin-cli-s3cr3t"

#: Toutes les commandes feuilles et un jeu d'arguments valides.
LEAVES = {
    ("user", "ensure"): ["--subject", "op|x"],
    ("org", "create"): ["--as", "op|x", "--name", "Acme"],
    ("org", "list"): ["--as", "op|x"],
    ("org", "show"): ["--as", "op|x", "--org", ORG],
    ("member", "add"): ["--as", "op|x", "--org", ORG, "--subject", "op|y", "--role", "viewer"],
    ("member", "list"): ["--as", "op|x", "--org", ORG],
    ("store", "create"): ["--as", "op|x", "--org", ORG, "--name", "S"],
    ("store", "list"): ["--as", "op|x", "--org", ORG],
    ("connection", "create"): ["--as", "op|x", "--org", ORG, "--store", STORE, "--label", "L"],
    ("service", "authorize"): ["--as", "op|x", "--org", ORG, "--service", "mervio_worker_1"],
    ("service", "revoke"): ["--as", "op|x", "--org", ORG, "--service", "mervio_worker_1"],
    ("service", "list"): ["--as", "op|x", "--org", ORG],
    ("job", "enqueue-import"): ["--as", "op|x", "--org", ORG, "--store", STORE, "--connection", STORE,
                                "--stripe", STORE],
    ("object", "upload"): ["--as", "op|x", "--org", ORG, "--store", STORE, "--kind", "stripe",
                           "--file", "/srv/s.csv"],
    ("job", "enqueue-analysis"): ["--as", "op|x", "--org", ORG, "--store", STORE, "--snapshot", STORE],
    ("job", "enqueue-purge"): ["--as", "op|x", "--org", ORG],
    ("job", "list"): ["--as", "op|x", "--org", ORG],
    ("job", "show"): ["--as", "op|x", "--org", ORG, "--job", STORE],
    ("job", "cancel"): ["--as", "op|x", "--org", ORG, "--job", STORE],
    ("job", "stats"): ["--as", "op|x", "--org", ORG],
    ("audit", "list"): ["--as", "op|x", "--org", ORG],
    ("demo", "provision"): ["--owner", "op|x", "--data-dir", "/tmp/mervio-demo"],
}


def parse(*argv):
    return build_parser().parse_args(["admin", *argv])


def run(*argv, environ=None, database_factory=None):
    out = io.StringIO()
    code = cli_admin.cmd_admin(parse(*argv), environ=environ if environ is not None else {}, stdout=out,
                               database_factory=database_factory)
    return code, out.getvalue()


# -- aide et analyse -----------------------------------------------------------------------------

def test_every_leaf_has_a_handler_and_accepts_its_arguments():
    assert set(cli_admin.HANDLERS) == {parse(*leaf, *argv).admin_handler for leaf, argv in LEAVES.items()}
    for leaf, argv in LEAVES.items():
        args = parse(*leaf, *argv, "--json")
        assert args.command == "admin" and args.json is True, leaf


@pytest.mark.parametrize("argv", [["admin"], ["admin", "org"], ["admin", "job"]] +
                         [["admin", *leaf] for leaf in LEAVES])
def test_help_is_available_at_every_level(argv, capsys):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args([*argv, "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage:")
    if len(argv) == 3:
        assert "Codes de sortie" in out.replace("\n", " ")


def test_help_runs_as_a_real_process_without_a_database():
    result = subprocess.run([sys.executable, "-m", "mervio.cli", "admin", "--help"], cwd=ROOT,
                            env={"PYTHONPATH": str(ROOT / "src")}, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "demo" in result.stdout and result.stderr == ""


@pytest.mark.parametrize("leaf", list(LEAVES))
def test_missing_required_arguments_are_usage_errors(leaf, capsys):
    with pytest.raises(SystemExit) as exit_info:
        parse(*leaf)
    assert exit_info.value.code == admin_errors.EXIT_USAGE
    assert "required" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    ["org", "show", "--as", "op|x", "--org", "not-a-uuid"],
    ["org", "show", "--as", "op|x", "--org", "' OR 1=1 --"],
    ["member", "add", "--as", "op|x", "--org", ORG, "--subject", "op|y", "--role", "owner"],
    ["member", "add", "--as", "op|x", "--org", ORG, "--subject", "op|y", "--role", "superuser"],
    ["job", "list", "--as", "op|x", "--org", ORG, "--status", "deleted"],
    ["job", "list", "--as", "op|x", "--org", ORG, "--type", "shell"],
    ["job", "list", "--as", "op|x", "--org", ORG, "--limit", "0"],
    ["job", "list", "--as", "op|x", "--org", ORG, "--limit", "1001"],
    ["job", "list", "--as", "op|x", "--org", ORG, "--limit", "ten"],
    ["job", "enqueue-purge", "--as", "op|x", "--org", ORG, "--priority", "101"],
    ["job", "enqueue-analysis", "--as", "op|x", "--org", ORG, "--store", STORE, "--grain", "day",
     "--snapshot", STORE],
    ["job", "enqueue-analysis", "--as", "op|x", "--org", ORG, "--store", STORE],
    ["job", "show", "--as", "op|x", "--org", ORG, "--job", "../../etc/passwd"],
    ["org", "frobnicate"],
    ["drop"],
])
def test_invalid_arguments_are_usage_errors(argv):
    with pytest.raises(SystemExit) as exit_info:
        parse(*argv)
    assert exit_info.value.code == admin_errors.EXIT_USAGE


def test_existing_commands_are_untouched():
    parser = build_parser()
    for command in ("analyze", "validate", "inspect", "demo", "worker"):
        assert parser.parse_args([command] + (["--file", "x"] if command == "inspect" else [])).command == command
    assert parser.parse_args(["worker", "healthcheck"]).worker_command == "healthcheck"


# -- configuration et erreurs -------------------------------------------------------------------

def test_a_missing_database_url_is_a_configuration_error_in_json(capsys):
    code, out = run("org", "list", "--as", "op|x", "--json")
    assert code == admin_errors.EXIT_USAGE
    document = json.loads(out)
    assert document["ok"] is False and document["error"]["code"] == "config"
    assert "MERVIO_DATABASE_URL" in document["error"]["message"]
    assert capsys.readouterr().err == ""


def test_an_invalid_database_url_never_echoes_its_value(capsys):
    for url in (f"mysql://user:{SECRET}@db/x", f"postgresql://:{SECRET}@/x", f"postgresql://u:{SECRET}@h:99999/x"):
        code, out = run("org", "list", "--as", "op|x", environ={"MERVIO_DATABASE_URL": url})
        err = capsys.readouterr().err
        assert code == admin_errors.EXIT_USAGE and out == ""
        assert err.startswith("erreur [config]: configuration invalide")
        assert SECRET not in err


def test_the_database_url_can_come_from_a_file(tmp_path):
    secret_file = tmp_path / "url"
    secret_file.write_text(f"postgresql://role:{SECRET}@127.0.0.1:5432/mervio\n", encoding="utf-8")
    settings = AdminSettings.from_env({"MERVIO_DATABASE_URL_FILE": str(secret_file)})
    assert settings.database_url.reveal().endswith("/mervio")
    assert SECRET not in repr(settings)
    with pytest.raises(SettingsError) as error:
        AdminSettings.from_env({"MERVIO_DATABASE_URL_FILE": "relative/url"})
    assert error.value.problems == [("MERVIO_DATABASE_URL_FILE", "chemin absolu attendu")]


class _Exploding:
    """Base factice: chaque operation leve l'exception fournie (aucune base reelle)."""

    def __init__(self, exc):
        self.exc = exc
        self.closed = False

    def __call__(self, url):
        assert SECRET in url
        return self

    def transaction(self, **_):
        raise self.exc

    def connection(self):
        raise self.exc

    def close(self):
        self.closed = True


@pytest.mark.parametrize("exc, code, name", [
    (RuntimeError(f"boom postgresql://u:{SECRET}@h/x"), admin_errors.EXIT_INTERNAL, "RuntimeError"),
    (KeyError(SECRET), admin_errors.EXIT_INTERNAL, "KeyError"),
])
def test_unexpected_errors_are_translated_and_never_published_raw(exc, code, name, capsys):
    factory = _Exploding(exc)
    status, out = run("org", "list", "--as", "op|x", "--json",
                      environ={"MERVIO_DATABASE_URL": f"postgresql://u:{SECRET}@127.0.0.1/x"},
                      database_factory=factory)
    assert status == code and factory.closed
    assert json.loads(out) == {"ok": False, "error": {"code": name, "message": "erreur interne"}}
    assert SECRET not in out + capsys.readouterr().err


def test_input_validation_happens_before_any_database_access():
    factory = _Exploding(AssertionError("la base ne doit pas etre touchee"))
    for argv in (["user", "ensure", "--subject", "service:worker"],
                 ["org", "create", "--as", "op|x", "--name", " padded"],
                 ["store", "create", "--as", "op|x", "--org", ORG, "--name", "S", "--currency", "eur"],
                 ["object", "upload", "--as", "op|x", "--org", ORG, "--store", STORE, "--kind", "stripe",
                  "--file", "/absent/nowhere-" + "z" * 12 + ".csv"],
                 ["job", "enqueue-import", "--as", "op|x", "--org", ORG, "--store", STORE, "--connection", STORE],
                 ["job", "enqueue-purge", "--as", "op|x", "--org", ORG, "--idempotency-key", "bad\nkey"],
                 ["job", "enqueue-analysis", "--as", "op|x", "--org", ORG, "--store", STORE, "--snapshot", STORE,
                  "--as-of", "yesterday"],
                 ["audit", "list", "--as", "op|x", "--org", ORG, "--action", "drop"],
                 ["service", "authorize", "--as", "op|x", "--org", ORG, "--service", "x; DROP ROLE y"],
                 ["demo", "provision", "--owner", "op|x", "--data-dir", "relative/dir"],
                 ["demo", "provision", "--owner", "op|x", "--data-dir", "/srv/../etc"],
                 ["demo", "provision", "--owner", "op|x", "--data-dir", str(ROOT / "data" / "sample")]):
        code, out = run(*argv, "--json", environ={"MERVIO_DATABASE_URL": f"postgresql://u:{SECRET}@h/x"},
                        database_factory=factory)
        assert code == admin_errors.EXIT_USAGE, (argv, out)
        assert json.loads(out)["error"]["code"] in ("invalid_input",), argv


def test_every_error_class_has_a_distinct_documented_exit_code():
    classes = (admin_errors.AdminError, admin_errors.InvalidInput, admin_errors.DatabaseUnavailable,
               admin_errors.SchemaMissing, admin_errors.NotFoundError, admin_errors.Forbidden,
               admin_errors.Conflict)
    assert [c.exit_code for c in classes] == [1, 2, 3, 4, 5, 6, 7]
    assert admin_errors.ConfigurationRefused.exit_code == admin_errors.EXIT_USAGE


def test_persistence_errors_translate_without_leaking():
    from mervio.persistence.errors import (
        JobStateError, NotFound, PermissionDenied, SchemaNotReady, TenantAccessDenied, UnsafeDatabaseConfiguration,
    )
    cases = {
        TenantAccessDenied(): (5, "not_found", "organization introuvable"),
        NotFound("store"): (5, "not_found", "store introuvable"),
        PermissionDenied("manage_stores", "viewer"): (6, "forbidden", "role viewer insuffisant pour manage_stores"),
        JobStateError("travail running: seul un travail en file s'annule"): (7, "invalid_state", None),
        SchemaNotReady("x"): (4, "schema_not_ready", None),
        UnsafeDatabaseConfiguration("connexion refusee"): (2, "config", None),
        ValueError("limit doit etre compris entre 1 et 1000"): (2, "invalid_input", None),
    }
    for exc, (code, name, message) in cases.items():
        translated = operations.translate(exc)
        assert (translated.exit_code, translated.code) == (code, name), exc
        if message is not None:
            assert translated.message == message


# -- rendu --------------------------------------------------------------------------------------

def test_text_rendering_is_deterministic_and_quotes_ambiguous_values():
    document = {"store": {"name": "Demo Store", "id": "s1", "currency": None},
                "status": "created",
                "stores": [{"name": "a=b", "id": "s2", "connections": [{"id": "c"}]}],
                "count": 3, "active": True}
    lines = cli_admin.render_text(document)
    assert lines == [
        "status: created",
        "active: true",
        "count: 3",
        'store: currency=- id=s1 name="Demo Store"',
        "stores: 1",
        '  - connections=[{"id": "c"}] id=s2 name="a=b"',
    ]
    assert cli_admin.render_text(document) == lines


def test_text_output_goes_to_stdout_and_errors_to_stderr(capsys):
    status = cli_admin.cmd_admin(parse("org", "list", "--as", "op|x"), environ={})
    captured = capsys.readouterr()
    assert status == admin_errors.EXIT_USAGE
    assert captured.out == "" and captured.err.startswith("erreur [config]:")


# -- jeu de demonstration -----------------------------------------------------------------------

def test_the_demo_dataset_is_reused_intact_and_repaired_atomically(tmp_path):
    directory = tmp_path / "demo"
    manifest = operations._demo_dataset(directory)  # noqa: SLF001 - comportement de fichiers, sans base
    assert manifest["synthetic"] is True and manifest["currency"] == "EUR"
    orders = directory / "shopify_orders.csv"
    before = {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in directory.iterdir()}

    # rejeu: rien n'est reecrit (un worker peut etre en train de lire)
    assert operations._demo_dataset(directory) == manifest  # noqa: SLF001
    assert {p.name: (p.stat().st_ino, p.stat().st_mtime_ns) for p in directory.iterdir()} == before

    # fichier altere: remplace par renommage (nouvel inode), contenu d'origine restaure
    original = orders.read_bytes()
    orders.write_bytes(b"tampered")
    reader = orders.open("rb")
    try:
        assert operations._demo_dataset(directory) == manifest  # noqa: SLF001
        assert reader.read() == b"tampered"  # un lecteur ouvert garde un fichier entier
    finally:
        reader.close()
    assert orders.read_bytes() == original
    assert orders.stat().st_ino != before["shopify_orders.csv"][0]
    assert not [p for p in directory.iterdir() if p.name.startswith(".mervio-demo-")]
    assert operations._is_demo_directory(directory)  # noqa: SLF001
