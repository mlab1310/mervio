#!/usr/bin/env python3
"""Porte CI: les migrations montent, descendent et remontent sur une base NEUVE (Mission 004.3).

    MERVIO_TEST_ADMIN_DATABASE_URL=postgresql://admin@127.0.0.1:5432/postgres \\
        PYTHONPATH=src python scripts/ci_migrations.py

Responsabilite unique: prouver, avec le vrai point d'entree `mervio.persistence.migrate`
et un role proprietaire NON superutilisateur (comme en production), que:

1. le serveur est bien PostgreSQL de la version majeure attendue (17 par defaut);
2. `upgrade head` atteint exactement la revision head;
3. `downgrade base` ne laisse AUCUN objet du schema (table, vue, sequence, fonction, type);
4. un second `upgrade head` reproduit le MEME schema (empreinte du catalogue: colonnes,
   contraintes, index, triggers, politiques RLS, fonctions, droits, RLS forcee).

La base et le role jetables `mervio_ci_*_<aleatoire>` sont crees puis supprimes, meme en cas
d'echec. Hote local obligatoire, sauf MERVIO_TEST_ALLOW_REMOTE_DATABASE=1 (regle de 004.1).

Codes de sortie: 0 porte franchie, 1 porte refusee, 2 configuration invalide.
"""
from __future__ import annotations

import os
import secrets
import sys
from dataclasses import dataclass
from typing import Callable, List, Optional, Protocol, Sequence, Tuple
from urllib.parse import quote

ADMIN_URL_ENV = "MERVIO_TEST_ADMIN_DATABASE_URL"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", ""}
DEFAULT_POSTGRES_MAJOR = 17

#: Empreinte du schema public. Aucune donnee: seulement la definition des objets.
FINGERPRINT_QUERIES = (
    ("colonnes",
     "SELECT table_name, column_name, data_type, numeric_precision, numeric_scale, is_nullable, column_default "
     "FROM information_schema.columns WHERE table_schema = 'public' ORDER BY 1, 2"),
    ("contraintes",
     "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(c.oid) FROM pg_constraint c "
     "JOIN pg_namespace n ON n.oid = c.connamespace WHERE n.nspname = 'public' ORDER BY 1, 2"),
    ("index",
     "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1"),
    ("triggers",
     "SELECT tgrelid::regclass::text, tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
     "JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
     "WHERE n.nspname = 'public' AND NOT t.tgisinternal ORDER BY 1, 2"),
    ("politiques",
     "SELECT tablename, policyname, permissive, roles::text, cmd, qual, with_check FROM pg_policies "
     "WHERE schemaname = 'public' ORDER BY 1, 2"),
    ("fonctions",
     "SELECT p.proname, pg_get_function_identity_arguments(p.oid), pg_get_functiondef(p.oid) FROM pg_proc p "
     "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' ORDER BY 1, 2"),
    ("tables",
     "SELECT relname, relkind, relrowsecurity, relforcerowsecurity, relacl::text FROM pg_class c "
     "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND relkind IN ('r', 'p', 'v') "
     "ORDER BY 1"),
    ("droits de colonne",
     "SELECT table_name, column_name, grantee, privilege_type FROM information_schema.column_privileges "
     "WHERE table_schema = 'public' AND grantee NOT IN (SELECT current_user) ORDER BY 1, 2, 3, 4"),
)

#: Objets qui ne doivent plus exister apres `downgrade base` (la table de version d'Alembic exceptee).
RESIDUE_QUERY = (
    "SELECT 'relation', c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname = 'public' AND c.relname NOT IN ('alembic_version', 'alembic_version_pkc') "
    "UNION ALL SELECT 'fonction', p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
    "WHERE n.nspname = 'public' "
    "UNION ALL SELECT 'type', t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
    "WHERE n.nspname = 'public' AND t.typtype IN ('e', 'd') "
    "ORDER BY 1, 2"
)


class ConfigurationError(Exception):
    """Environnement de verification invalide (URL absente, hote distant, serveur inattendu)."""


class MigrationTarget(Protocol):
    def upgrade(self, revision: str) -> None: ...
    def downgrade(self, revision: str) -> None: ...
    def current(self) -> Optional[str]: ...
    def head(self) -> str: ...
    def fingerprint(self) -> Tuple: ...
    def residue(self) -> List[Tuple[str, str]]: ...


def verify_round_trip(target: MigrationTarget, log: Callable[[str], None] = print) -> List[str]:
    """Montee, descente complete, remontee. Renvoie les raisons de refus (vide = succes)."""
    reasons: List[str] = []
    head = target.head()

    def step(label: str, action: Callable[[], None]) -> bool:
        try:
            action()
        except Exception as exc:  # noqa: BLE001 - toute erreur de migration est un refus
            reasons.append(f"{label} en echec: {type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
            return False
        log(f"ci_migrations: {label} ok")
        return True

    if not step("upgrade head", lambda: target.upgrade("head")):
        return reasons
    if target.current() != head:
        reasons.append(f"apres upgrade: revision {target.current()!r}, head attendue {head!r}")
        return reasons
    first = target.fingerprint()

    if not step("downgrade base", lambda: target.downgrade("base")):
        return reasons
    if target.current() is not None:
        reasons.append(f"apres downgrade base: revision {target.current()!r} au lieu d'aucune")
    residue = target.residue()
    if residue:
        reasons.append("objets restants apres downgrade base: "
                       + ", ".join(f"{kind} {name}" for kind, name in residue[:20]))

    if not step("second upgrade head", lambda: target.upgrade("head")):
        return reasons
    if target.current() != head:
        reasons.append(f"apres second upgrade: revision {target.current()!r}, head attendue {head!r}")
    second = target.fingerprint()
    if second != first:
        differing = [label for (label, _), a, b in zip(FINGERPRINT_QUERIES, first, second) if a != b]
        reasons.append("schema different apres un aller-retour: " + ", ".join(differing or ["empreinte"]))
    return reasons


# -- cible PostgreSQL reelle -------------------------------------------------------------------

@dataclass
class PostgresTarget:
    url: str

    def upgrade(self, revision: str) -> None:
        from mervio.persistence import migrate
        migrate.upgrade(self.url, revision)

    def downgrade(self, revision: str) -> None:
        from mervio.persistence import migrate
        migrate.downgrade(self.url, revision)

    def current(self) -> Optional[str]:
        from mervio.persistence import migrate
        return migrate.current_revision(self.url)

    def head(self) -> str:
        from mervio.persistence import migrate
        return migrate.head_revision()

    def _query(self, sql: str) -> list:
        import psycopg
        with psycopg.connect(self.url, autocommit=True) as conn:
            return conn.execute(sql).fetchall()

    def fingerprint(self) -> Tuple:
        return tuple(tuple(self._query(sql)) for _, sql in FINGERPRINT_QUERIES)

    def residue(self) -> List[Tuple[str, str]]:
        return [tuple(row) for row in self._query(RESIDUE_QUERY)]


def admin_url(environ=os.environ) -> str:
    url = environ.get(ADMIN_URL_ENV)
    if not url:
        raise ConfigurationError(f"{ADMIN_URL_ENV} non definie")
    from psycopg.conninfo import conninfo_to_dict
    try:
        host = conninfo_to_dict(url).get("host", "")
    except Exception:  # noqa: BLE001 - une URL invalide est une erreur de configuration
        raise ConfigurationError(f"{ADMIN_URL_ENV} invalide") from None
    if (host not in LOCAL_HOSTS and not host.startswith("/")
            and environ.get("MERVIO_TEST_ALLOW_REMOTE_DATABASE") != "1"):
        raise ConfigurationError(f"{ADMIN_URL_ENV} vise un hote non local: refus par securite")
    return url


def check_server(version_num: int, expected_major: int) -> None:
    if version_num // 10000 != expected_major:
        raise ConfigurationError(f"PostgreSQL {version_num // 10000} detecte, {expected_major} attendu")


def run(admin: str, *, expected_major: int = DEFAULT_POSTGRES_MAJOR,
        log: Callable[[str], None] = print) -> List[str]:
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    suffix = secrets.token_hex(5)
    role, database, password = f"mervio_ci_migrator_{suffix}", f"mervio_ci_migrations_{suffix}", secrets.token_hex(16)
    params = conninfo_to_dict(admin)
    host = params.get("host") or "127.0.0.1"
    host = f"[{host}]" if ":" in host else host
    url = f"postgresql://{role}:{quote(password, safe='')}@{host}:{params.get('port', '5432')}/{database}"

    with psycopg.connect(admin, autocommit=True) as conn:
        check_server(int(conn.execute("SHOW server_version_num").fetchone()[0]), expected_major)
        log(f"ci_migrations: PostgreSQL {conn.info.server_version // 10000} joignable")
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS CREATEROLE PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)))
        try:
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(database), sql.Identifier(role)))
            try:
                superuser, bypass = conn.execute(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
                if superuser or bypass:  # pragma: no cover - cree NOSUPERUSER NOBYPASSRLS juste au-dessus
                    return ["le role de migration est privilegie"]
                return verify_round_trip(PostgresTarget(url), log)
            finally:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))
        finally:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def main(argv: Optional[Sequence[str]] = None, environ=os.environ) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    expected_major = DEFAULT_POSTGRES_MAJOR
    if args:
        if len(args) != 2 or args[0] != "--postgres-major" or not args[1].isdigit():
            print("usage: ci_migrations.py [--postgres-major N]", file=sys.stderr)
            return 2
        expected_major = int(args[1])
    try:
        reasons = run(admin_url(environ), expected_major=expected_major)
    except ConfigurationError as exc:
        print(f"ci_migrations: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - base injoignable, droits insuffisants: refus explicite
        print(f"REFUS: verification impossible: {type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
        return 1
    if reasons:
        for reason in reasons:
            print(f"REFUS: {reason}")
        return 1
    print("ci_migrations: porte franchie")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
