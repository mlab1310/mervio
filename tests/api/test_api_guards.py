"""Les trois gardes structurelles de l'API (Mission 004.6, ADR-004.6-002).

Elles sont posees AVANT la premiere route metier, parce que les reposer apres coup
signifierait supprimer du code qui fonctionne. Chacune est verifiee par l'AST, donc elle
n'attrape pas un comportement observe mais une FORME: on ne peut pas l'oublier en ecrivant une
route, et aucune donnee de test n'est necessaire pour la prouver.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

API = pathlib.Path(__file__).resolve().parents[2] / "src" / "mervio" / "api"

#: noms de parametre par lesquels un client pourrait designer l'organisation. L'API ne les
#: accepte jamais: elle derive l'organisation de l'appartenance (ADR-004.6-001).
CLIENT_ORGANIZATION_NAMES = {"organization_id", "organisation_id", "org_id", "organization", "tenant_id"}


def api_modules():
    assert API.is_dir(), f"{API} absent: le squelette de l'API n'existe pas"
    return sorted(API.rglob("*.py"))


def imported_roots(tree: ast.AST):
    """Racines de tous les modules importes: `a.b.c` -> `a`, et `from a.b import c` -> `a`."""
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def imported_dotted(tree: ast.AST):
    """Noms complets, pour distinguer `mervio.analytics` de `mervio.persistence`."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


@pytest.fixture(scope="module")
def parsed():
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in api_modules()]


# -- garde 1: aucune route ne calcule de KPI ---------------------------------------------------

def test_no_module_of_the_api_imports_the_analytics_engine(parsed):
    """L'API est un LECTEUR: elle projette ce que le moteur a produit et que la persistance a
    scelle. Elle ne derive aucun KPI, aucun score, aucune contribution. Exigence explicite de
    la roadmap 004.6."""
    for path, tree in parsed:
        offending = {name for name in imported_dotted(tree)
                     if name == "mervio.analytics" or name.startswith("mervio.analytics.")}
        assert not offending, f"{path.name} importe le moteur analytique: {sorted(offending)}"
        assert "analytics" not in {n for n in imported_roots(tree)}, path.name


# -- garde 2: aucun rapport persiste n'est valide contre le schema 2.0 -------------------------

def test_no_module_of_the_api_imports_jsonschema_or_the_report_schema(parsed):
    """Le schema valide ce que le moteur PRODUIT; il refuserait un rapport 1.x legitimement
    persiste, et `reports` est immuable (handoff 004.5, §9.2). Un rapport 1.x doit rester
    SERVI."""
    for path, tree in parsed:
        assert "jsonschema" not in imported_roots(tree), f"{path.name} importe jsonschema"
        source = path.read_text(encoding="utf-8")
        for forbidden in ("schemas/report", "urn:mervio:schema:report"):
            assert forbidden not in source, f"{path.name} designe le fichier de schema: {forbidden}"


# -- garde 3: l'organisation ne vient jamais du client -----------------------------------------

def test_the_api_never_takes_the_organization_from_a_client_parameter(parsed):
    """<< Organisation TOUJOURS derivee de l'appartenance, jamais d'un parametre client >>
    (roadmap 004.6). Une garde AST rend la regle STRUCTURELLE au lieu de disciplinaire."""
    for path, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
            offending = names & CLIENT_ORGANIZATION_NAMES
            assert not offending, (
                f"{path.name}:{node.lineno} {node.name}() accepte {sorted(offending)}: "
                "l'organisation se derive de l'appartenance")


def test_no_route_path_carries_an_organization_segment(parsed):
    """Corollaire de la garde 3: aucun chemin d'URL ne porte l'organisation."""
    for path, tree in parsed:
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            for name in CLIENT_ORGANIZATION_NAMES:
                assert f"{{{name}}}" not in node.value, f"{path.name}:{node.lineno} chemin {node.value!r}"
