"""Frontieres d'architecture de la persistance (Mission 004.1, ADR-004-001). Sans base de donnees.

- le moteur (domain, ingestion, analytics) et les autres paquets existants n'importent jamais la
  persistance ni un driver de base: ils restent utilisables et testables sans PostgreSQL;
- le SQL (psycopg) ne vit que dans `mervio.persistence` et le cas d'usage persiste;
- SQLAlchemy n'est utilise que par les migrations Alembic;
- le coeur reste sans dependance runtime (pyproject).
"""
from __future__ import annotations

import ast
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "mervio"
DATABASE_MODULES = ("psycopg", "psycopg_pool", "sqlalchemy", "alembic")


def _imports(path: Path):
    names = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                package = path.relative_to(SRC.parent).parent.parts
                base = package[: len(package) - (node.level - 1)]
                module = ".".join(base + ((module,) if module else ()))
            names.append(module)
    return names


def _python_files(*packages):
    for package in packages:
        yield from (SRC / package).rglob("*.py")


def test_engine_and_existing_packages_never_import_persistence_or_database_drivers():
    for path in _python_files("domain", "ingestion", "analytics", "llm", "reporting", "synthetic", "cli"):
        for name in _imports(path):
            assert not name.startswith("mervio.persistence"), (path, name)
            assert not name.startswith("mervio.application.persisted_analysis"), (path, name)
            assert name.split(".")[0] not in DATABASE_MODULES, (path, name)
    for path in (SRC / "config.py", SRC / "errors.py", SRC / "logging_config.py", SRC / "__init__.py"):
        assert not any(n.split(".")[0] in DATABASE_MODULES for n in _imports(path)), path


def test_application_package_stays_importable_without_database_dependencies():
    for path in (SRC / "application").glob("*.py"):
        if path.name == "persisted_analysis.py":
            continue
        for name in _imports(path):
            assert name.split(".")[0] not in DATABASE_MODULES and not name.startswith("mervio.persistence"), (path, name)


def test_sql_driver_is_confined_to_the_persistence_layer():
    allowed = {SRC / "application" / "persisted_analysis.py"}
    for path in SRC.rglob("*.py"):
        if "persistence" in path.relative_to(SRC).parts or path in allowed:
            continue
        assert not any(n.split(".")[0] in DATABASE_MODULES for n in _imports(path)), path


def test_sqlalchemy_is_only_used_by_migrations():
    allowed = {SRC / "persistence" / "migrate.py", SRC / "persistence" / "migrations" / "env.py"}
    for path in SRC.rglob("*.py"):
        if any(n.split(".")[0] == "sqlalchemy" for n in _imports(path)):
            assert path in allowed, path


def test_persistence_never_reimplements_analytics():
    """La persistance transporte le Dataset et le rapport; elle n'importe aucun module de calcul."""
    forbidden = ("mervio.analytics.kpi", "mervio.analytics.health", "mervio.analytics.anomaly",
                 "mervio.analytics.root_cause", "mervio.analytics.insights", "mervio.analytics.profitability",
                 "mervio.analytics.timeseries", "mervio.analytics.pipeline")
    for path in (SRC / "persistence").rglob("*.py"):
        for name in _imports(path):
            assert not name.startswith(forbidden), (path, name)


def test_core_package_keeps_zero_runtime_dependencies():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == []
    assert any(d.startswith("psycopg") for d in project["optional-dependencies"]["persistence"])


def test_cli_and_engine_import_without_persistence_modules_loaded():
    code = ("import sys; import mervio.cli.main, mervio.analytics, mervio.application; "
            "loaded = [m for m in sys.modules if m.split('.')[0] in ('psycopg', 'sqlalchemy', 'alembic') "
            "or m.startswith('mervio.persistence')]; print(loaded); assert not loaded")
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                            env={"PYTHONPATH": str(ROOT / "src")})
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_credential_is_committed_with_the_persistence_setup():
    """Aucune URL avec mot de passe reel; les variables d'exemple sont vides."""
    import re
    credential_url = re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:/@\s]+:(?!<)[^@\s]+@")
    files = list((SRC / "persistence").rglob("*.py")) + list((ROOT / "tests" / "persistence").glob("*.py"))
    files += [SRC / "application" / "persisted_analysis.py", ROOT / "docker-compose.yml", ROOT / ".env.example",
              ROOT / "scripts" / "dev_postgres.sh"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not credential_url.search(text), path
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            assert line.endswith("="), line
    assert "${MERVIO_POSTGRES_PASSWORD:?" in (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def test_sql_fstrings_only_interpolate_reviewed_constants():
    """Injection SQL: une valeur n'entre jamais dans le texte SQL, seulement en parametre (%s).

    Les seules interpolations autorisees dans une chaine SQL sont des listes de colonnes constantes et des noms de
    table issus d'une liste blanche du module. Toute nouvelle interpolation fait echouer ce test et impose une revue.
    """
    import re
    sql_keyword = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|RETURNING)\b")
    allowed = {"_RUN_COLUMNS", "_REPORT_COLUMNS", "_SNAPSHOT_COLUMNS", "_STORE_COLUMNS", "_CONNECTION_COLUMNS",
               # snapshots._RowWriter._insert / _RowReader._rows et provenance.trace_record: constantes d'appel
               "table", "column_list", "arrays", "columns", "order"}
    for path in (SRC / "persistence").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.JoinedStr):
                continue
            literal = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if not sql_keyword.search(literal):
                continue
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    assert isinstance(value.value, ast.Name) and value.value.id in allowed, (
                        path.name, node.lineno, ast.unparse(value.value))
