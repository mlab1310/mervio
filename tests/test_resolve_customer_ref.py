"""`mervio worker resolve-customer-ref` sans base de donnees (Mission 004.4.5 E6, D-062).

Ce qui se prouve ICI, sans PostgreSQL: le cadrage de l'entree (`parse_identity`), le contrat de
STDOUT, l'absence de toute variante argv de l'identite, et les codes de sortie des refus.

La resolution reelle contre une vraie base -- determinisme, liaison a l'organisation, absence de
mutation, effacement de bout en bout -- est couverte par
tests/persistence/test_customer_resolution.py.
"""
from __future__ import annotations

import ast
import inspect
import io
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from mervio.cli import build_parser
from mervio.cli import worker as cli_worker
from mervio.workers import resolution

ROOT = Path(__file__).resolve().parent.parent
ORG = "3f0c7c1e-8a55-4d7e-9d0b-6a1f2e0b9c11"
SECRET = "Zq9-e6-s3cr3t"
MASTER_HEX = "b3" * 32
EMAIL = "Alice@Example.COM"


def _source(obj) -> str:
    """Source desindentee: `ast.parse` refuse un corps de fonction encore indente."""
    return textwrap.dedent(inspect.getsource(obj))


def parse(*argv):
    return build_parser().parse_args(["worker", "resolve-customer-ref", *argv])


def run(*argv, stdin=b"", environ=None):
    """Execute la commande en capturant les DEUX flux separement: stdout est un contrat."""
    out, err = io.StringIO(), io.StringIO()
    code = cli_worker.cmd_resolve_customer_ref(
        parse(*argv), environ={} if environ is None else environ,
        stdin=io.BytesIO(stdin), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


# -- cadrage de l'entree: `parse_identity` ------------------------------------------------------

def test_a_trailing_newline_is_accepted_because_echo_adds_one():
    assert resolution.parse_identity(b"alice@example.com\n") == "alice@example.com"
    assert resolution.parse_identity(b"alice@example.com\r\n") == "alice@example.com"


@pytest.mark.parametrize("raw", [b"  alice@example.com  ", b"\talice@example.com\t", b"\n alice@example.com \n"])
def test_surrounding_whitespace_is_removed_like_the_import_does(raw):
    assert resolution.parse_identity(raw) == "alice@example.com"


@pytest.mark.parametrize("raw", [b"", b"\n", b"   ", b"\t\r\n "])
def test_an_empty_identity_is_refused(raw):
    with pytest.raises(resolution.ResolutionRefused) as refused:
        resolution.parse_identity(raw)
    assert refused.value.code == "identity_empty"


@pytest.mark.parametrize("raw", [b"a@b.c\nd@e.f", b"a@b.c\nd@e.f\n", b"a@b.c\r\nd@e.f"])
def test_two_lines_are_refused_instead_of_silently_resolving_one(raw):
    """Deux lignes designeraient deux identites: en resoudre une en silence rendrait la sortie
    ambigue precisement la ou l'operateur va effacer."""
    with pytest.raises(resolution.ResolutionRefused) as refused:
        resolution.parse_identity(raw)
    assert refused.value.code == "identity_not_single_line"


def test_an_oversized_identity_is_refused_and_never_truncated():
    at_limit = b"a" * (resolution.MAX_IDENTITY_BYTES - 10) + b"@example.com"
    assert len(at_limit) > resolution.MAX_IDENTITY_BYTES
    with pytest.raises(resolution.ResolutionRefused) as refused:
        resolution.parse_identity(at_limit)
    assert refused.value.code == "identity_too_long"
    assert resolution.parse_identity(b"a" * resolution.MAX_IDENTITY_BYTES) == "a" * resolution.MAX_IDENTITY_BYTES


def test_bytes_that_are_not_utf8_are_refused():
    """L'import calcule le HMAC sur de l'UTF-8: d'autres octets ne peuvent designer aucune reference."""
    with pytest.raises(resolution.ResolutionRefused) as refused:
        resolution.parse_identity(b"alice@\xff\xfeexample.com")
    assert refused.value.code == "identity_not_utf8"


def test_unicode_survives_with_exactly_the_import_normalization():
    """Ni NFC, ni NFKC, ni repli ASCII: `normalize_email` retire les blancs et met en minuscules,
    et rien d'autre. Normaliser autrement donnerait une reference qui n'existe nulle part."""
    assert resolution.parse_identity(" ÉLÈVE@Exemple.FR \n".encode("utf-8")) == "ÉLÈVE@Exemple.FR"
    # « e » + accent combinant: conserve TEL QUEL, jamais recompose en NFC, sinon la reference
    # calculee ici differerait de celle que l'import a persistee pour les memes octets
    composed, combining = "é@x.fr", "é@x.fr"
    assert resolution.parse_identity(combining.encode("utf-8")) == combining
    assert resolution.parse_identity(composed.encode("utf-8")) == composed
    assert composed != combining


def test_the_stream_framing_never_replaces_the_derivation_normalization():
    """`parse_identity` ne met PAS en minuscules: c'est `ref_email` qui normalise, une seule fois."""
    assert resolution.parse_identity(b"Alice@Example.COM") == "Alice@Example.COM"


def test_no_email_syntax_is_required_because_the_import_requires_none():
    """Exiger ici une syntaxe que l'ingestion n'exige pas rendrait INEFFACABLE un client importe."""
    for odd in (b"pas-un-email", b"a@b", b"a b@c.d", b"@", b"\"x\"@y.z"):
        assert resolution.parse_identity(odd) == odd.decode("utf-8")


# -- stdin, jamais argv -------------------------------------------------------------------------

def test_no_option_anywhere_in_the_cli_can_carry_an_identity():
    """D-062: aucune variante `--email`, `--customer-email` ni `--identity`, pas meme en option.
    Un argument est lisible par `ps`, conserve par l'historique du shell et capture par les traces."""
    forbidden = ("--email", "--customer-email", "--identity", "--mail", "--address", "--subject-email")
    text = build_parser().format_help()
    assert not any(option in text for option in forbidden)
    for option in forbidden:
        with pytest.raises(SystemExit):
            parse("--org", ORG, "--as", "op|x", option, EMAIL)


def test_the_resolver_accepts_only_the_organization_and_the_actor():
    args = parse("--org", ORG, "--as", "op|x")
    assert (args.org, args.actor, args.worker_command) == (ORG, "op|x", "resolve-customer-ref")
    for missing in (["--org", ORG], ["--as", "op|x"]):
        with pytest.raises(SystemExit):
            parse(*missing)


def test_the_identity_comes_from_the_stream_and_the_arguments_carry_only_org_and_actor():
    """Garde statique: la commande ne lit dans `args` que `org` et `actor`. L'identite vient du
    FLUX, jamais d'un argument -- ce que D-062 impose, et que `ps` rend verifiable."""
    source = _source(cli_worker.cmd_resolve_customer_ref)
    from_arguments = {node.attr for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Attribute)
                      and isinstance(node.value, ast.Name) and node.value.id == "args"}
    assert from_arguments == {"org", "actor"}
    assert "_read_identity(stdin" in source


# -- contrat de stdout --------------------------------------------------------------------------

def test_a_refusal_writes_nothing_at_all_on_stdout():
    """STDOUT EST UN CONTRAT: l'operateur le passe a `enqueue-redact --customer-ref`. De la prose
    ou un message d'erreur y deviendrait une reference invalide."""
    code, out, err = run("--org", ORG, "--as", "op|x", stdin=b"\n",
                         environ={"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
                                  "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX})
    assert (code, out) == (2, "")
    assert err.strip() == "resolution refusee: identity_empty"


def test_a_refusal_never_echoes_the_identity_nor_any_secret():
    environ = {"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
               "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX}
    code, out, err = run("--org", "pas-un-uuid", "--as", "op|x", stdin=EMAIL.encode(), environ=environ)
    assert (code, out) == (2, "")
    for leak in (EMAIL, EMAIL.lower(), "alice", "example.com", SECRET, MASTER_HEX):
        assert leak not in err
    assert err.strip() == "resolution refusee: organization_invalid"


def test_an_unreachable_database_is_refused_without_naming_the_secret():
    code, out, err = run("--org", ORG, "--as", "op|x", stdin=EMAIL.encode(),
                         environ={"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
                                  "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX})
    assert (code, out) == (3, "")
    assert err.strip() == "resolution refusee: database_unavailable"
    assert SECRET not in err


def test_a_missing_master_key_is_refused_before_any_identity_leaves_the_process():
    """Frontiere de D-062: sans cle maitre, il n'y a rien a resoudre. La variable est nommee (c'est
    actionnable), jamais une valeur."""
    code, out, err = run("--org", ORG, "--as", "op|x", stdin=EMAIL.encode(),
                         environ={"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x"})
    assert (code, out) == (2, "")
    assert err.strip() == "resolution refusee: config_invalid:MERVIO_IDENTITY_MASTER_KEY"


def test_the_library_refuses_a_missing_master_key_even_if_settings_let_one_through():
    """Defense en profondeur: la fonction de resolution ne derive rien sans cle maitre, meme
    appelee directement."""
    with pytest.raises(resolution.ResolutionRefused) as refused:
        resolution.resolve_customer_ref(object(), organization_id=ORG, actor_subject="op|x",
                                        identity="alice@example.com", master=None)
    assert refused.value.code == "identity_master_key_missing"


def test_the_resolution_needs_neither_an_object_store_nor_a_health_file():
    """Une resolution ne lit aucun objet brut et n'ecrit aucune sante: exiger ces variables ferait
    echouer, pour une raison etrangere, une commande realisable (meme regle que HealthCheckSettings)."""
    settings = resolution.load_settings({"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
                                         "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX})
    assert settings.identity_master_key.reveal() == MASTER_HEX
    assert not hasattr(settings, "object_store") and not hasattr(settings, "health_file")
    assert SECRET not in repr(settings) and MASTER_HEX not in repr(settings)


def test_a_malformed_master_key_is_refused_without_showing_it():
    code, out, err = run("--org", ORG, "--as", "op|x", stdin=EMAIL.encode(),
                         environ={"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
                                  "MERVIO_IDENTITY_MASTER_KEY": "zz" * 32})
    assert (code, out) == (2, "")
    assert "zz" not in err.replace("MERVIO_IDENTITY_MASTER_KEY", "")
    assert err.strip() == "resolution refusee: config_invalid:MERVIO_IDENTITY_MASTER_KEY"


def test_an_invalid_configuration_names_only_variables():
    code, out, err = run("--org", ORG, "--as", "op|x", stdin=EMAIL.encode(), environ={})
    assert (code, out) == (2, "")
    assert err.startswith("resolution refusee: config_invalid:") and "MERVIO_DATABASE_URL" in err


def test_every_refusal_class_maps_to_a_documented_exit_code():
    assert set(cli_worker._EXIT_BY_KIND) == {resolution.REFUSED, resolution.CONFIG, resolution.DATABASE,
                                            resolution.SCHEMA, resolution.INTERNAL}
    # tous les refus de RESOLUTION partagent un code: la sortie ne doit pas devenir un oracle
    assert cli_worker._EXIT_BY_KIND[resolution.REFUSED] == cli_worker._EXIT_BY_KIND[resolution.CONFIG] == 2
    assert (cli_worker._EXIT_BY_KIND[resolution.DATABASE], cli_worker._EXIT_BY_KIND[resolution.SCHEMA]) == (3, 4)


def test_the_help_documents_stdin_and_the_two_step_operator_path(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["worker", "resolve-customer-ref", "--help"])
    text = capsys.readouterr().out
    assert "STDIN" in text and "enqueue-redact" in text
    assert "0 resolu" in text and "4 schema non migre" in text


# -- silence ------------------------------------------------------------------------------------

def test_the_resolver_configures_no_logging_at_all():
    """D-062 n'audite pas la resolution: un log -- meme sans l'identite -- ferait de stderr la
    trace de l'exercice d'une capacite de reidentification."""
    tree = ast.parse(_source(cli_worker.cmd_resolve_customer_ref))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & {"configure", "get_event_logger", "_refusal_logger", "configure_logging"}
    assert "log" not in {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_the_resolution_module_writes_nothing_and_enqueues_nothing():
    """Garde statique sur le CODE (pas sur la prose, qui nomme justement ce qui est interdit):
    aucun SQL d'ecriture, aucun audit, aucune mise en file, aucun `ensure_identity_key`.
    Resoudre ne doit RIEN initialiser (D-062)."""
    tree = ast.parse(_source(resolution))
    literals = [node.value for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    statements = [text for text in literals if re.search(r"\b(SELECT|INSERT|UPDATE|DELETE)\b", text)]
    # le module ne porte AUCUN SQL: il delegue la seule lecture a `load_identity_key`
    assert statements == []
    referenced = ({node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                  | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                  | {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                     for alias in node.names})
    assert not referenced & {"ensure_identity_key", "audit", "record", "enqueue", "uuid4", "new_salt"}
    assert "load_identity_key" in referenced


def test_the_resolver_is_deterministic_by_construction_and_never_random():
    """E6 n'utilise pas `uuid4`: la reference est un HMAC, pas un tirage."""
    for module in (resolution, cli_worker):
        tree = ast.parse(_source(module))
        imported = {(node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                     for alias in node.names}
        assert not imported & {"random", "secrets"}, module.__name__
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
        assert not names & {"uuid4", "token_bytes", "token_hex", "new_salt"}, module.__name__


# -- le processus reel --------------------------------------------------------------------------

def test_the_real_command_line_refuses_an_empty_stdin_with_a_silent_stdout():
    variables = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    variables.update({"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
                      "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX,
                      "PYTHONPATH": str(ROOT / "src")})
    result = subprocess.run([sys.executable, "-m", "mervio.cli", "worker", "resolve-customer-ref",
                             "--org", ORG, "--as", "op|x"], cwd=ROOT, input=b"",
                            capture_output=True, env=variables, timeout=60)
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr.decode().strip() == "resolution refusee: identity_empty"


def test_the_identity_never_appears_in_the_process_arguments():
    """Preuve operationnelle de « stdin, jamais argv »: l'identite n'est dans aucun argument, donc
    elle n'est pas lisible par `ps` ni conservee par l'historique du shell."""
    variables = {key: value for key, value in os.environ.items() if not key.startswith("MERVIO_")}
    variables.update({"MERVIO_DATABASE_URL": f"postgresql://svc:{SECRET}@127.0.0.1:1/x",
                      "MERVIO_IDENTITY_MASTER_KEY": MASTER_HEX,
                      "PYTHONPATH": str(ROOT / "src")})
    argv = [sys.executable, "-m", "mervio.cli", "worker", "resolve-customer-ref", "--org", ORG, "--as", "op|x"]
    assert not any(EMAIL in argument for argument in argv)
    result = subprocess.run(argv, cwd=ROOT, input=EMAIL.encode(), capture_output=True,
                            env=variables, timeout=60)
    # la base est injoignable: l'identite a ete acceptee, jamais affichee
    assert result.returncode == 3 and result.stdout == b""
    assert EMAIL not in result.stderr.decode() and EMAIL.lower() not in result.stderr.decode()
