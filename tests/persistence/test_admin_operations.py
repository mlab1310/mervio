"""`mervio admin` contre PostgreSQL reel (Mission 004.3.7).

Chaque commande passe par le vrai point d'entree (`cli.admin.cmd_admin`) avec le role applicatif
reel (ni superutilisateur ni BYPASSRLS). Les comptages globaux et les verifications d'isolation
lisent la base avec le role `bypass`, reserve aux preuves.

Couvert: autorisation par role, isolation des tenants (aucune fuite d'existence), audit (acteur,
action, ressource, ajout seul), idempotence et concurrence du provisionnement, autorisations de
service, travaux, entrees hostiles, secrets et chemins dans les sorties, refus de configuration.
"""
from __future__ import annotations

import io
import json
import threading
import uuid

import psycopg
import pytest

from mervio.admin import errors as admin_errors
from mervio.admin import operations
from mervio.cli import admin as cli_admin
from mervio.cli import build_parser
from mervio.persistence import jobs, service
from mervio.persistence.database import Database

from .service_support import raw

OWNER_A, ADMIN_A, ANALYST_A, VIEWER_A = "op|a-owner", "op|a-admin", "op|a-analyst", "op|a-viewer"
OWNER_B, OUTSIDER = "op|b-owner", "op|outsider"


# =============================================================================================
# Outillage
# =============================================================================================

class Admin:
    """`mervio admin ... --json` en processus, sur le role de connexion choisi."""

    def __init__(self, pg, object_root):
        self.pg = pg
        #: racine du magasin d'objets de ce test (004.4.4): `object upload` y depose les octets
        self.object_root = object_root
        self.outputs = []

    def __call__(self, *argv, kind="app", database=None, url=None):
        args = build_parser().parse_args(["admin", *argv, "--json"])
        out = io.StringIO()
        environ = {"MERVIO_DATABASE_URL": url or self.pg.url(kind, database),
                   "MERVIO_OBJECT_STORE_ROOT": str(self.object_root)}
        code = cli_admin.cmd_admin(args, environ=environ, stdout=out)
        self.outputs.append(out.getvalue())
        document = json.loads(out.getvalue())
        assert document["ok"] is (code == 0), document
        return code, (document["result"] if code == 0 else document["error"])

    def ok(self, *argv, **options):
        code, result = self(*argv, **options)
        assert code == 0, result
        return result


@pytest.fixture
def admin(db, pg, tmp_path):
    runner = Admin(pg, tmp_path / "objects")
    yield runner
    published = "".join(runner.outputs)
    for secret in pg.passwords.values():
        assert secret not in published
    assert "postgresql://" not in published


@pytest.fixture
def evidence(db, pg):
    """SQL brut sous BYPASSRLS: comptages globaux et preuves d'isolation, jamais utilise par le produit."""
    with psycopg.connect(pg.url("bypass"), autocommit=True) as conn:
        yield conn


def count(conn, sql, *params):
    return conn.execute(sql, params).fetchone()[0]


@pytest.fixture
def world(admin, tmp_path):
    """Organisation A (proprietaire, admin, analyste, lecteur), organisation B, un humain exterieur."""
    for subject in (OWNER_A, ADMIN_A, ANALYST_A, VIEWER_A, OWNER_B, OUTSIDER):
        admin.ok("user", "ensure", "--subject", subject)
    org_a = admin.ok("org", "create", "--as", OWNER_A, "--name", "Org A")["organization"]["id"]
    org_b = admin.ok("org", "create", "--as", OWNER_B, "--name", "Org B")["organization"]["id"]
    for subject, role in ((ADMIN_A, "admin"), (ANALYST_A, "analyst"), (VIEWER_A, "viewer")):
        admin.ok("member", "add", "--as", OWNER_A, "--org", org_a, "--subject", subject, "--role", role)
    store_a = admin.ok("store", "create", "--as", ADMIN_A, "--org", org_a, "--name", "Store A",
                       "--currency", "EUR")["store"]["id"]
    store_b = admin.ok("store", "create", "--as", OWNER_B, "--org", org_b, "--name", "Store B")["store"]["id"]
    conn_a = admin.ok("connection", "create", "--as", ADMIN_A, "--org", org_a, "--store", store_a,
                      "--label", "CSV A")["connection"]["id"]
    conn_b = admin.ok("connection", "create", "--as", OWNER_B, "--org", org_b, "--store", store_b,
                      "--label", "CSV B")["connection"]["id"]
    source = tmp_path / "orders.csv"
    source.write_text(SAMPLE_CSV, encoding="utf-8")
    object_a = admin.ok("object", "upload", "--as", ANALYST_A, "--org", org_a, "--store", store_a,
                        "--kind", "shopify_orders", "--file", str(source))["object"]["id"]
    return {"a": org_a, "b": org_b, "store_a": store_a, "store_b": store_b, "conn_a": conn_a,
            "conn_b": conn_b, "object_a": object_a}


def user_id(evidence, subject):
    return evidence.execute("SELECT id FROM users WHERE idp_subject = %s", (subject,)).fetchone()[0]


def events(evidence, organization, action=None):
    rows = evidence.execute(
        "SELECT action, actor_type, actor_id, on_behalf_of, resource_type, resource_id, store_id, metadata "
        "FROM audit_events WHERE organization_id = %s AND (%s::text IS NULL OR action = %s) "
        "ORDER BY created_at, id", (organization, action, action)).fetchall()
    return rows


#: CSV minimal accepte par le connecteur Shopify: seul le PARCOURS est teste ici.
SAMPLE_CSV = ("Name,Created at,Lineitem quantity,Lineitem name,Lineitem price,Total\n"
              "#1001,2026-01-05 10:00:00 +0000,1,Widget,10.00,10.00\n")


def import_args(world, *extra, as_=ANALYST_A, org=None, raw_object=None):
    """004.4.4: la mise en file designe un OBJET BRUT, plus jamais un chemin (D-054)."""
    return ("job", "enqueue-import", "--as", as_, "--org", org or world["a"], "--store", world["store_a"],
            "--connection", world["conn_a"], "--shopify-orders", raw_object or world["object_a"], *extra)


# =============================================================================================
# Identites
# =============================================================================================

def test_user_ensure_is_idempotent_and_never_creates_a_duplicate(admin, evidence):
    first = admin.ok("user", "ensure", "--subject", "op|someone")
    again = admin.ok("user", "ensure", "--subject", "op|someone")
    assert first["status"] == "created" and again["status"] == "existing"
    assert first["user"] == again["user"] == {"id": first["user"]["id"], "subject": "op|someone"}
    assert count(evidence, "SELECT count(*) FROM users WHERE idp_subject = 'op|someone'") == 1
    assert count(evidence, "SELECT count(*) FROM users WHERE kind = 'human'") == 1


@pytest.mark.parametrize("subject", ["service:mervio_app", " op|padded", "op|tab\there", "op|nul\x00",
                                     "op|\u202eevil", "x" * 256])
def test_user_ensure_refuses_reserved_or_hostile_subjects(admin, evidence, subject):
    code, error = admin("user", "ensure", "--subject", subject)
    assert (code, error["code"]) == (admin_errors.EXIT_USAGE, "invalid_input")
    assert count(evidence, "SELECT count(*) FROM users") == 0


def test_an_unknown_actor_is_refused_without_creating_anything(admin, evidence):
    code, error = admin("org", "create", "--as", "op|typo", "--name", "Nope")
    assert (code, error["code"]) == (admin_errors.EXIT_NOT_FOUND, "actor_not_found")
    assert count(evidence, "SELECT count(*) FROM users") == 0
    assert count(evidence, "SELECT count(*) FROM organizations") == 0


def test_a_service_principal_cannot_act_as_an_administrator(admin, world, principal):
    code, error = admin("org", "list", "--as", principal.subject)
    assert (code, error["code"]) == (admin_errors.EXIT_DENIED, "forbidden")
    code, error = admin("org", "show", "--as", principal.subject, "--org", world["a"])
    assert code == admin_errors.EXIT_DENIED


# =============================================================================================
# Organisations
# =============================================================================================

def test_org_create_makes_the_actor_owner_and_is_audited(admin, evidence):
    admin.ok("user", "ensure", "--subject", OWNER_A)
    created = admin.ok("org", "create", "--as", OWNER_A, "--name", "Acme")
    organization = created["organization"]["id"]
    assert created["status"] == "created" and created["role"] == "owner"
    owner_id = user_id(evidence, OWNER_A)
    assert evidence.execute("SELECT user_id, role FROM memberships WHERE organization_id = %s",
                            (organization,)).fetchall() == [(owner_id, "owner")]
    assert events(evidence, organization) == [
        ("organization.created", "user", owner_id, None, "organization", uuid.UUID(organization), None,
         {"owner_id": str(owner_id)})]


def test_org_create_is_idempotent_per_owner_and_exact_name(admin, evidence):
    for subject in (OWNER_A, OWNER_B):
        admin.ok("user", "ensure", "--subject", subject)
    first = admin.ok("org", "create", "--as", OWNER_A, "--name", "Acme")
    again = admin.ok("org", "create", "--as", OWNER_A, "--name", "Acme")
    assert again == {**first, "status": "existing"}
    other_owner = admin.ok("org", "create", "--as", OWNER_B, "--name", "Acme")
    other_name = admin.ok("org", "create", "--as", OWNER_A, "--name", "acme")
    assert other_owner["status"] == other_name["status"] == "created"
    assert len({first["organization"]["id"], other_owner["organization"]["id"],
                other_name["organization"]["id"]}) == 3
    assert count(evidence, "SELECT count(*) FROM organizations") == 3
    assert count(evidence, "SELECT count(*) FROM audit_events WHERE action = 'organization.created'") == 3


def test_a_member_who_is_not_owner_gets_a_new_organization_of_the_same_name(admin, world, evidence):
    created = admin.ok("org", "create", "--as", ADMIN_A, "--name", "Org A")
    assert created["status"] == "created" and created["organization"]["id"] != world["a"]


def test_concurrent_org_creation_creates_exactly_one_organization(db, pg, evidence):
    operations.ensure_user(db, OWNER_A)
    results, failures = [], []
    barrier = threading.Barrier(6)

    def create():
        database = Database(pg.url("app"))
        try:
            barrier.wait()
            results.append(operations.create_organization(database, actor=OWNER_A, name="Race"))
        except Exception as exc:  # noqa: BLE001 - rapporte au fil principal
            failures.append(exc)
        finally:
            database.close()

    threads = [threading.Thread(target=create) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not failures
    assert sorted(r["status"] for r in results) == ["created"] + ["existing"] * 5
    assert len({r["organization"]["id"] for r in results}) == 1
    assert count(evidence, "SELECT count(*) FROM organizations") == 1
    assert count(evidence, "SELECT count(*) FROM audit_events") == 1


def test_org_list_shows_only_the_actor_memberships(admin, world):
    listed = admin.ok("org", "list", "--as", VIEWER_A)["organizations"]
    assert listed == [{"id": world["a"], "name": "Org A", "role": "viewer"}]
    assert admin.ok("org", "list", "--as", OUTSIDER) == {"organizations": []}


def test_org_show_is_scoped_by_role(admin, world, principal):
    admin.ok("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    for_viewer = admin.ok("org", "show", "--as", VIEWER_A, "--org", world["a"])
    assert for_viewer["role"] == "viewer" and "services" not in for_viewer
    assert [s["id"] for s in for_viewer["stores"]] == [world["store_a"]]
    assert [c["id"] for c in for_viewer["stores"][0]["connections"]] == [world["conn_a"]]
    assert for_viewer["jobs"]["jobs_total"] == 0
    for_owner = admin.ok("org", "show", "--as", OWNER_A, "--org", world["a"])
    assert [s["service"] for s in for_owner["services"]] == [principal.name]


def test_a_foreign_organization_is_indistinguishable_from_a_missing_one(admin, world):
    """Aucune fuite d'existence: memes code, meme message, meme document."""
    missing = str(uuid.uuid4())
    commands = (
        ("org", "show"), ("store", "list"), ("member", "list"), ("job", "list"), ("job", "stats"),
        ("audit", "list"), ("service", "list"),
    )
    for command in commands:
        foreign = admin(*command, "--as", OWNER_A, "--org", world["b"])
        absent = admin(*command, "--as", OWNER_A, "--org", missing)
        assert foreign == absent == (admin_errors.EXIT_NOT_FOUND,
                                     {"code": "not_found", "message": "organization introuvable"}), command
    for command in (("store", "create", "--name", "Intrusion"),
                    ("connection", "create", "--store", world["store_b"], "--label", "Intrusion"),
                    ("member", "add", "--subject", OUTSIDER, "--role", "admin"),
                    ("service", "authorize", "--service", "anything"),
                    ("job", "enqueue-purge")):
        foreign = admin(*command[:2], "--as", OWNER_A, "--org", world["b"], *command[2:])
        absent = admin(*command[:2], "--as", OWNER_A, "--org", missing, *command[2:])
        assert foreign == absent and foreign[0] == admin_errors.EXIT_NOT_FOUND, command


def test_nothing_is_written_in_a_foreign_organization(admin, world, evidence):
    before = {table: count(evidence, f"SELECT count(*) FROM {table} WHERE organization_id = %s", world["b"])
              for table in ("stores", "connections", "memberships", "jobs", "audit_events",
                            "service_authorizations")}
    admin("store", "create", "--as", OWNER_A, "--org", world["b"], "--name", "Intrusion")
    admin("member", "add", "--as", OWNER_A, "--org", world["b"], "--subject", OWNER_A, "--role", "admin")
    admin("connection", "create", "--as", OWNER_A, "--org", world["b"], "--store", world["store_b"],
          "--label", "Intrusion")
    admin(*import_args(world, org=world["b"], as_=OWNER_A))
    admin("job", "enqueue-purge", "--as", OWNER_A, "--org", world["b"])
    after = {table: count(evidence, f"SELECT count(*) FROM {table} WHERE organization_id = %s", world["b"])
             for table in before}
    assert after == before


# =============================================================================================
# Membres
# =============================================================================================

def test_member_add_is_idempotent_audited_and_refuses_a_role_change(admin, world, evidence):
    again = admin.ok("member", "add", "--as", OWNER_A, "--org", world["a"], "--subject", VIEWER_A,
                     "--role", "viewer")
    assert again["status"] == "existing" and again["membership"]["role"] == "viewer"
    code, error = admin("member", "add", "--as", OWNER_A, "--org", world["a"], "--subject", VIEWER_A,
                        "--role", "admin")
    assert (code, error["code"]) == (admin_errors.EXIT_CONFLICT, "role_mismatch")
    added = events(evidence, world["a"], "member.added")
    assert len(added) == 3
    owner_id = user_id(evidence, OWNER_A)
    assert {(row[1], row[2], row[3], row[4]) for row in added} == {("user", owner_id, None, "membership")}
    assert sorted(row[7]["role"] for row in added) == ["admin", "analyst", "viewer"]
    roles = {m["subject"]: m["role"] for m in admin.ok("member", "list", "--as", OWNER_A,
                                                        "--org", world["a"])["members"]}
    assert roles == {OWNER_A: "owner", ADMIN_A: "admin", ANALYST_A: "analyst", VIEWER_A: "viewer"}


@pytest.mark.parametrize("actor", [ADMIN_A, ANALYST_A, VIEWER_A])
def test_only_the_owner_manages_members_and_existence_is_not_revealed(admin, world, evidence, actor):
    for subject in (OUTSIDER, "op|does-not-exist"):
        code, error = admin("member", "add", "--as", actor, "--org", world["a"], "--subject", subject,
                            "--role", "viewer")
        assert (code, error["code"]) == (admin_errors.EXIT_DENIED, "forbidden")
    assert admin("member", "list", "--as", actor, "--org", world["a"])[0] == admin_errors.EXIT_DENIED
    assert count(evidence, "SELECT count(*) FROM memberships WHERE organization_id = %s", world["a"]) == 4


def test_privilege_escalation_to_owner_is_not_possible_from_the_cli(admin, world, evidence):
    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(["admin", "member", "add", "--as", OWNER_A, "--org", world["a"],
                                   "--subject", OUTSIDER, "--role", "owner"])
    assert exit_info.value.code == 2
    with pytest.raises(admin_errors.InvalidInput):
        operations.add_member(Database(admin.pg.url("app")), actor=OWNER_A, organization=world["a"],
                              subject=OUTSIDER, role="owner")
    # un administrateur ne s'ajoute pas lui-meme un meilleur role
    code, _ = admin("member", "add", "--as", ADMIN_A, "--org", world["a"], "--subject", ADMIN_A, "--role", "admin")
    assert code == admin_errors.EXIT_DENIED
    assert count(evidence, "SELECT count(*) FROM memberships WHERE role = 'owner'") == 2


def test_an_unknown_member_subject_is_not_found_for_the_owner(admin, world):
    code, error = admin("member", "add", "--as", OWNER_A, "--org", world["a"], "--subject", "op|ghost",
                        "--role", "viewer")
    assert (code, error) == (admin_errors.EXIT_NOT_FOUND, {"code": "not_found", "message": "user introuvable"})


# =============================================================================================
# Boutiques et connexions
# =============================================================================================

def test_store_creation_is_audited_idempotent_and_detects_a_currency_conflict(admin, world, evidence):
    again = admin.ok("store", "create", "--as", OWNER_A, "--org", world["a"], "--name", "Store A",
                     "--currency", "EUR")
    assert again["status"] == "existing" and again["store"]["id"] == world["store_a"]
    no_currency = admin.ok("store", "create", "--as", OWNER_A, "--org", world["a"], "--name", "Store A")
    assert no_currency["store"]["id"] == world["store_a"]
    code, error = admin("store", "create", "--as", OWNER_A, "--org", world["a"], "--name", "Store A",
                        "--currency", "USD")
    assert (code, error["code"]) == (admin_errors.EXIT_CONFLICT, "currency_mismatch")
    created = events(evidence, world["a"], "store.created")
    assert created == [("store.created", "user", user_id(evidence, ADMIN_A), None, "store",
                        uuid.UUID(world["store_a"]), uuid.UUID(world["store_a"]), {"currency": "EUR"})]
    assert count(evidence, "SELECT count(*) FROM stores WHERE organization_id = %s", world["a"]) == 1


@pytest.mark.parametrize("actor", [ANALYST_A, VIEWER_A, OUTSIDER])
def test_store_and_connection_creation_require_admin(admin, world, evidence, actor):
    expected = admin_errors.EXIT_NOT_FOUND if actor == OUTSIDER else admin_errors.EXIT_DENIED
    # meme pour un nom existant: le role est verifie avant de dire "existe deja"
    for name in ("Store A", "New"):
        assert admin("store", "create", "--as", actor, "--org", world["a"], "--name", name)[0] == expected
    for label in ("CSV A", "New"):
        assert admin("connection", "create", "--as", actor, "--org", world["a"], "--store", world["store_a"],
                     "--label", label)[0] == expected
    assert count(evidence, "SELECT count(*) FROM stores") == 2
    assert count(evidence, "SELECT count(*) FROM connections") == 2


def test_concurrent_store_creation_creates_exactly_one_store(world, pg, evidence):
    results, failures = [], []
    barrier = threading.Barrier(5)

    def create():
        database = Database(pg.url("app"))
        try:
            barrier.wait()
            results.append(operations.create_store(database, actor=ADMIN_A, organization=world["a"],
                                                   name="Concurrent", currency="EUR"))
        except Exception as exc:  # noqa: BLE001
            failures.append(exc)
        finally:
            database.close()

    threads = [threading.Thread(target=create) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not failures
    assert sorted(r["status"] for r in results) == ["created"] + ["existing"] * 4
    assert count(evidence, "SELECT count(*) FROM stores WHERE name = 'Concurrent'") == 1
    assert count(evidence, "SELECT count(*) FROM audit_events WHERE action = 'store.created' "
                           "AND organization_id = %s", world["a"]) == 2


def test_connection_creation_is_idempotent_audited_and_scoped_to_the_store(admin, world, evidence):
    again = admin.ok("connection", "create", "--as", OWNER_A, "--org", world["a"], "--store", world["store_a"],
                     "--label", "CSV A")
    assert again["status"] == "existing" and again["connection"]["id"] == world["conn_a"]
    # la boutique d'une autre organisation n'existe pas ici
    code, error = admin("connection", "create", "--as", OWNER_A, "--org", world["a"], "--store", world["store_b"],
                        "--label", "CSV A")
    assert (code, error["message"]) == (admin_errors.EXIT_NOT_FOUND, "store introuvable")
    created = events(evidence, world["a"], "connection.created")
    assert created == [("connection.created", "user", user_id(evidence, ADMIN_A), None, "connection",
                        uuid.UUID(world["conn_a"]), uuid.UUID(world["store_a"]), {"kind": "csv_upload"})]


def test_a_revoked_connection_is_not_reused_and_cannot_import(admin, world, db, evidence):
    from mervio.persistence import stores
    from mervio.persistence.tenancy import TenantContext, TenantSession
    session = TenantSession(db, TenantContext(uuid.UUID(world["a"]), user_id(evidence, OWNER_A)))
    stores.revoke_connection(session, uuid.UUID(world["store_a"]), uuid.UUID(world["conn_a"]))
    code, error = admin(*import_args(world))
    assert (code, error["code"]) == (admin_errors.EXIT_CONFLICT, "connection_revoked")
    replacement = admin.ok("connection", "create", "--as", ADMIN_A, "--org", world["a"],
                           "--store", world["store_a"], "--label", "CSV A")
    assert replacement["status"] == "created" and replacement["connection"]["id"] != world["conn_a"]


# =============================================================================================
# Autorisations de service
# =============================================================================================

def test_service_authorization_lifecycle_is_audited_and_effective(admin, world, evidence, worker_db, principal):
    granted = admin.ok("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    assert granted["status"] == "created" and granted["authorization"]["active"] is True
    assert granted["authorization"]["service"] == principal.name
    assert service.authorized_organizations(worker_db, principal) == [uuid.UUID(world["a"])]
    replay = admin.ok("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    assert replay["status"] == "existing" and replay["authorization"]["id"] == granted["authorization"]["id"]

    revoked = admin.ok("service", "revoke", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    assert revoked["authorization"]["active"] is False
    assert service.authorized_organizations(worker_db, principal) == []
    code, error = admin("service", "revoke", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    assert (code, error["message"]) == (admin_errors.EXIT_NOT_FOUND, "service_authorization introuvable")

    owner_id = user_id(evidence, OWNER_A)
    traced = [row for row in events(evidence, world["a"]) if row[0].startswith("service.")]
    authorization_id = uuid.UUID(granted["authorization"]["id"])
    assert traced == [
        ("service.authorized", "user", owner_id, None, "service_authorization", authorization_id, None,
         {"service_id": str(principal.id)}),
        ("service.revoked", "user", owner_id, None, "service_authorization", authorization_id, None,
         {"service_id": str(principal.id)}),
    ]
    listed = admin.ok("service", "list", "--as", OWNER_A, "--org", world["a"], "--include-revoked")
    assert [a["active"] for a in listed["authorizations"]] == [False]
    assert admin.ok("service", "list", "--as", OWNER_A, "--org", world["a"]) == {"authorizations": []}
    again = admin.ok("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    assert again["status"] == "created" and again["authorization"]["id"] != granted["authorization"]["id"]


@pytest.mark.parametrize("actor", [ADMIN_A, ANALYST_A, VIEWER_A])
def test_only_the_owner_authorizes_a_service_and_service_existence_is_not_revealed(
        admin, world, evidence, principal, actor):
    for name in (principal.name, "no_such_service"):
        for action in ("authorize", "revoke"):
            code, error = admin("service", action, "--as", actor, "--org", world["a"], "--service", name)
            assert (code, error["code"]) == (admin_errors.EXIT_DENIED, "forbidden"), (name, action)
    assert admin("service", "list", "--as", actor, "--org", world["a"])[0] == admin_errors.EXIT_DENIED
    assert count(evidence, "SELECT count(*) FROM service_authorizations") == 0


def test_an_unknown_service_is_reported_to_the_owner(admin, world):
    code, error = admin("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", "ghost_worker")
    assert (code, error["code"]) == (admin_errors.EXIT_NOT_FOUND, "service_not_found")
    code, _ = admin("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", "bad name;--")
    assert code == admin_errors.EXIT_USAGE


def test_an_authorization_in_one_organization_grants_nothing_in_another(admin, world, worker_db, principal):
    admin.ok("service", "authorize", "--as", OWNER_B, "--org", world["b"], "--service", principal.name)
    code, _ = admin("service", "authorize", "--as", OWNER_B, "--org", world["a"], "--service", principal.name)
    assert code == admin_errors.EXIT_NOT_FOUND
    assert service.authorized_organizations(worker_db, principal) == [uuid.UUID(world["b"])]


# =============================================================================================
# Travaux
# =============================================================================================

def test_enqueue_import_is_audited_idempotent_and_carries_no_path(admin, world, evidence):
    """004.4.4: le chemin n'est plus REDIGE a l'affichage, il n'existe plus du tout (D-054).

    Avant cette mission, la charge utile portait le vrai chemin en base et ne le masquait qu'au
    rendu. Desormais elle ne transporte que des identifiants d'objets bruts.
    """
    created = admin.ok(*import_args(world, "--idempotency-key", "import-1", "--priority", "5"))
    replay = admin.ok(*import_args(world, "--idempotency-key", "import-1"))
    assert created["status"] == "created" and replay["status"] == "existing"
    assert replay["job"]["id"] == created["job"]["id"]
    job = created["job"]
    assert (job["job_type"], job["status"], job["priority"], job["store_id"]) == (
        "import", "queued", 5, world["store_a"])
    # `raw_objects` tombe sous le fragment de cle `raw` de `redaction.py`: la vue rendue le
    # masque (defense en profondeur, conservatrice), la charge persistee reste verifiable.
    assert job["payload"]["raw_objects"] == "[redacted]"
    assert "sources" not in job["payload"]
    assert job["enqueued_by"] == str(user_id(evidence, ANALYST_A))

    stored = evidence.execute("SELECT payload FROM jobs WHERE id = %s", (job["id"],)).fetchone()[0]
    assert stored["raw_objects"] == {"shopify_orders": world["object_a"]}
    # la preuve centrale: AUCUNE valeur de la charge utile persistee ne ressemble a un chemin
    assert "/" not in json.dumps(stored), stored
    assert count(evidence, "SELECT count(*) FROM jobs") == 1
    enqueued = events(evidence, world["a"], "job.enqueued")
    assert len(enqueued) == 1
    assert enqueued[0][1:5] == ("user", user_id(evidence, ANALYST_A), None, "job")


def test_an_upload_is_audited_without_the_local_filename(admin, world, evidence, tmp_path):
    """L'audit d'un depot porte l'empreinte et la taille, jamais le nom du fichier local."""
    source = tmp_path / "client-secret-name-orders.csv"
    source.write_text(SAMPLE_CSV, encoding="utf-8")
    uploaded = admin.ok("object", "upload", "--as", ANALYST_A, "--org", world["a"],
                        "--store", world["store_a"], "--kind", "stripe", "--file", str(source))
    assert uploaded["status"] == "created"
    assert set(uploaded["object"]) == {"id", "store_id", "source_kind", "origin", "sha256",
                                       "byte_size", "state", "retain_until", "created_at"}
    assert uploaded["object"]["state"] == "available"
    assert uploaded["object"]["origin"] == "csv_upload"
    traced = events(evidence, world["a"], "object.uploaded")
    assert len(traced) == 2  # celui de `world` (shopify_orders) et celui-ci (stripe)
    metadata = json.dumps([row[7] for row in traced])
    assert "client-secret-name" not in metadata and "/" not in metadata, metadata
    assert uploaded["object"]["sha256"] in metadata


@pytest.mark.parametrize("path", ["/srv/../etc/passwd", "/srv/./orders.csv", "/srv//orders.csv",
                                  "/srv/orders.csv\x00.txt", "/" + "a" * 1100, "relative/orders.csv",
                                  "~/orders.csv"])
def test_a_hostile_local_path_is_refused_at_upload(admin, world, evidence, path):
    """Le chemin local est refuse la ou il est encore lu: au depot. Il n'atteint jamais un travail."""
    before = count(evidence, "SELECT count(*) FROM raw_objects")
    code, error = admin("object", "upload", "--as", ANALYST_A, "--org", world["a"],
                        "--store", world["store_a"], "--kind", "shopify_orders", "--file", path)
    assert (code, error["code"]) == (admin_errors.EXIT_USAGE, "invalid_input")
    assert path not in json.dumps(error), error  # le chemin refuse n'est pas renvoye
    assert count(evidence, "SELECT count(*) FROM raw_objects") == before
    assert count(evidence, "SELECT count(*) FROM jobs") == 0


@pytest.mark.parametrize("value", ["/srv/mervio/orders.csv", "relative/orders.csv", "not-a-uuid"])
def test_a_path_can_never_be_passed_as_a_raw_object(admin, world, evidence, value):
    """Meme en visant le drapeau de source, un chemin n'est pas un identifiant d'objet.

    L'analyseur d'arguments le refuse AVANT toute logique: sortie 2, aucun travail cree.
    """
    with pytest.raises(SystemExit) as exit_code:
        admin(*import_args(world, raw_object=value))
    assert exit_code.value.code == admin_errors.EXIT_USAGE
    assert count(evidence, "SELECT count(*) FROM jobs") == 0


def test_enqueue_import_needs_a_source_and_the_right_role(admin, world, evidence):
    code, _ = admin("job", "enqueue-import", "--as", ANALYST_A, "--org", world["a"], "--store", world["store_a"],
                    "--connection", world["conn_a"])
    assert code == admin_errors.EXIT_USAGE
    code, error = admin(*import_args(world, as_=VIEWER_A))
    assert (code, error) == (admin_errors.EXIT_DENIED,
                             {"code": "forbidden", "message": "role viewer insuffisant pour import_data"})
    code, error = admin("job", "enqueue-import", "--as", ANALYST_A, "--org", world["a"], "--store", world["store_a"],
                        "--connection", world["conn_b"], "--shopify-orders", world["object_a"])
    assert (code, error["message"]) == (admin_errors.EXIT_NOT_FOUND, "connection introuvable")
    assert count(evidence, "SELECT count(*) FROM jobs") == 0


def test_job_inspection_and_cancellation(admin, world, evidence):
    job = admin.ok(*import_args(world))["job"]
    listed = admin.ok("job", "list", "--as", VIEWER_A, "--org", world["a"], "--status", "queued")["jobs"]
    assert [j["id"] for j in listed] == [job["id"]] and "payload" not in listed[0]
    assert admin.ok("job", "list", "--as", VIEWER_A, "--org", world["a"], "--type", "analysis") == {"jobs": []}
    assert admin.ok("job", "stats", "--as", VIEWER_A, "--org", world["a"])["statistics"]["by_status"] == \
        {"queued": 1}
    assert admin("job", "cancel", "--as", VIEWER_A, "--org", world["a"], "--job", job["id"])[0] == \
        admin_errors.EXIT_DENIED
    cancelled = admin.ok("job", "cancel", "--as", ANALYST_A, "--org", world["a"], "--job", job["id"])
    assert cancelled["job"]["status"] == "cancelled"
    code, error = admin("job", "cancel", "--as", ANALYST_A, "--org", world["a"], "--job", job["id"])
    assert (code, error["code"]) == (admin_errors.EXIT_CONFLICT, "invalid_state")
    cancel_events = events(evidence, world["a"], "job.cancelled")
    assert [row[1:3] for row in cancel_events] == [("user", user_id(evidence, ANALYST_A))]
    # le travail d'une autre organisation n'existe pas pour son proprietaire
    for command in ("show", "cancel"):
        code, error = admin("job", command, "--as", OWNER_B, "--org", world["b"], "--job", job["id"])
        assert (code, error["message"]) == (admin_errors.EXIT_NOT_FOUND, "job introuvable")


def test_enqueue_analysis_validates_its_snapshot(admin, world):
    job = admin.ok(*import_args(world))["job"]
    code, error = admin("job", "enqueue-analysis", "--as", ANALYST_A, "--org", world["a"],
                        "--store", world["store_a"], "--from-import", job["id"])
    assert (code, error["code"]) == (admin_errors.EXIT_CONFLICT, "import_not_succeeded")
    code, error = admin("job", "enqueue-analysis", "--as", ANALYST_A, "--org", world["a"],
                        "--store", world["store_a"], "--snapshot", str(uuid.uuid4()))
    assert (code, error["message"]) == (admin_errors.EXIT_NOT_FOUND, "snapshot introuvable")
    code, _ = admin("job", "enqueue-analysis", "--as", ANALYST_A, "--org", world["a"],
                    "--store", world["store_a"], "--snapshot", str(uuid.uuid4()), "--as-of", "2026-13-01")
    assert code == admin_errors.EXIT_USAGE
    code, _ = admin("job", "enqueue-analysis", "--as", VIEWER_A, "--org", world["a"],
                    "--store", world["store_a"], "--from-import", job["id"])
    assert code == admin_errors.EXIT_DENIED
    for argv in ([], ["--snapshot", str(uuid.uuid4()), "--from-import", job["id"]]):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["admin", "job", "enqueue-analysis", "--as", ANALYST_A, "--org", world["a"],
                                       "--store", world["store_a"], *argv])


def test_enqueue_purge_is_reserved_to_the_owner(admin, world, evidence):
    for actor in (ADMIN_A, ANALYST_A, VIEWER_A):
        assert admin("job", "enqueue-purge", "--as", actor, "--org", world["a"])[0] == admin_errors.EXIT_DENIED
    purge = admin.ok("job", "enqueue-purge", "--as", OWNER_A, "--org", world["a"], "--idempotency-key", "p1")
    assert (purge["job"]["job_type"], purge["job"]["payload"]) == ("purge", {"retention": {}})
    assert count(evidence, "SELECT count(*) FROM jobs") == 1


# =============================================================================================
# Audit
# =============================================================================================

def test_audit_list_is_for_admins_and_scoped_to_the_organization(admin, world, evidence):
    admin.ok(*import_args(world))
    for actor in (ANALYST_A, VIEWER_A):
        assert admin("audit", "list", "--as", actor, "--org", world["a"])[0] == admin_errors.EXIT_DENIED
    listed = admin.ok("audit", "list", "--as", ADMIN_A, "--org", world["a"], "--limit", "1000")["events"]
    assert [e["action"] for e in listed] == [
        "organization.created", "member.added", "member.added", "member.added", "store.created",
        "connection.created", "object.uploaded", "job.enqueued"]
    assert all(e["actor_type"] == "user" and e["on_behalf_of"] is None for e in listed)
    ids_b = {str(r[0]) for r in evidence.execute("SELECT id FROM audit_events WHERE organization_id = %s",
                                                 (world["b"],))}
    assert ids_b and not ids_b & {e["id"] for e in listed}
    filtered = admin.ok("audit", "list", "--as", ADMIN_A, "--org", world["a"], "--action", "store.created")
    assert [e["action"] for e in filtered["events"]] == ["store.created"]
    code, _ = admin("audit", "list", "--as", ADMIN_A, "--org", world["a"], "--action", "drop table")
    assert code == admin_errors.EXIT_USAGE


def test_every_admin_event_names_the_real_actor(admin, world, evidence):
    """L'acteur vient de l'appartenance verifiee: aucune option ne permet de le choisir."""
    rows = evidence.execute("SELECT action, actor_type, actor_id, on_behalf_of FROM audit_events "
                            "WHERE organization_id = %s", (world["a"],)).fetchall()
    expected = {"organization.created": OWNER_A, "member.added": OWNER_A, "store.created": ADMIN_A,
                "connection.created": ADMIN_A, "object.uploaded": ANALYST_A}
    assert {action for action, *_ in rows} == set(expected)
    for action, actor_type, actor_id, on_behalf_of in rows:
        assert (actor_type, actor_id, on_behalf_of) == ("user", user_id(evidence, expected[action]), None)
    options = {action.dest for action in build_parser()._actions}  # noqa: SLF001 - introspection d'argparse
    assert not {"actor_id", "actor_type", "on_behalf_of"} & options


def test_admin_audit_events_are_append_only(admin, world, app_conn):
    with pytest.raises(psycopg.Error):
        with raw(app_conn, world["a"]) as conn:
            conn.execute("UPDATE audit_events SET actor_id = NULL WHERE action = 'store.created'")
    with raw(app_conn, world["a"]) as conn:
        deleted = conn.execute("DELETE FROM audit_events WHERE action = 'organization.created'").rowcount
    assert deleted == 0  # plancher de retention: une trace recente ne se supprime pas


# =============================================================================================
# Entrees hostiles, secrets, configuration
# =============================================================================================

def test_sql_like_input_is_stored_literally(admin, evidence):
    hostile = "x'); DROP TABLE organizations; --"
    admin.ok("user", "ensure", "--subject", "op|'; DELETE FROM users; --")
    created = admin.ok("org", "create", "--as", "op|'; DELETE FROM users; --", "--name", hostile)
    assert created["organization"]["name"] == hostile
    assert evidence.execute("SELECT name FROM organizations").fetchall() == [(hostile,)]
    assert count(evidence, "SELECT count(*) FROM users") == 1


@pytest.mark.parametrize("name", ["", " padded", "x" * 201, "line\nbreak", "bell\x07"])
def test_hostile_names_are_refused_before_any_write(admin, world, evidence, name):
    code, _ = admin("store", "create", "--as", ADMIN_A, "--org", world["a"], "--name", name)
    assert code == admin_errors.EXIT_USAGE
    code, _ = admin("store", "create", "--as", ADMIN_A, "--org", world["a"], "--name", "ok", "--currency", "eur")
    assert code == admin_errors.EXIT_USAGE
    assert count(evidence, "SELECT count(*) FROM stores") == 2


def test_a_superuser_or_bypassrls_connection_is_refused(admin, world, evidence):
    code, error = admin("org", "list", "--as", OWNER_A, kind="bypass")
    assert (code, error["code"]) == (admin_errors.EXIT_USAGE, "config")
    assert "BYPASSRLS" in error["message"]


def test_the_worker_role_cannot_provision(admin, world, principal, evidence):
    """Un role de service reste soumis a ses politiques: aucune ecriture de tenant par la CLI."""
    admin.ok("service", "authorize", "--as", OWNER_A, "--org", world["a"], "--service", principal.name)
    code, error = admin("store", "create", "--as", OWNER_A, "--org", world["a"], "--name", "By worker",
                        kind="worker")
    assert code in (admin_errors.EXIT_NOT_FOUND, admin_errors.EXIT_DENIED), error
    code, error = admin("org", "create", "--as", OWNER_A, "--name", "By worker", kind="worker")
    assert code in (admin_errors.EXIT_NOT_FOUND, admin_errors.EXIT_DENIED), error
    assert count(evidence, "SELECT count(*) FROM stores WHERE name = 'By worker'") == 0
    assert count(evidence, "SELECT count(*) FROM organizations WHERE name = 'By worker'") == 0


def test_an_unreachable_database_is_reported_without_its_url(admin):
    url = "postgresql://someone:" + "Zq9-admin-s3cr3t" + "@127.0.0.1:1/nowhere"
    code, error = admin("org", "list", "--as", OWNER_A, url=url)
    assert (code, error["code"]) == (admin_errors.EXIT_DATABASE, "database_unavailable")
    assert "Zq9-admin-s3cr3t" not in admin.outputs[-1] and "someone" not in admin.outputs[-1]


def test_an_unmigrated_database_is_reported_as_such(admin, pg):
    name = pg.create_empty_database("admin_schema")
    try:
        code, error = admin("org", "list", "--as", OWNER_A, database=name)
        assert code == admin_errors.EXIT_SCHEMA, error
    finally:
        pg.drop_database(name)


def test_a_database_without_the_admin_audit_vocabulary_is_reported_as_unmigrated(admin, pg):
    from mervio.persistence import migrate
    name = pg.create_empty_database("admin_0007")
    try:
        migrate.upgrade(pg.url("migrator", name), "0007_service_identity")
        admin.ok("user", "ensure", "--subject", OWNER_A, database=name)
        code, error = admin("org", "create", "--as", OWNER_A, "--name", "Too early", database=name)
        assert (code, error["code"]) == (admin_errors.EXIT_SCHEMA, "schema_not_ready")
        with psycopg.connect(pg.url("bypass", name), autocommit=True) as conn:
            assert count(conn, "SELECT count(*) FROM organizations") == 0
    finally:
        pg.drop_database(name)


def test_a_payload_carrying_a_path_can_no_longer_be_enqueued_at_all(world, db, evidence):
    """004.4.4: la defense a remonte d'un cran.

    Auparavant une charge utile ecrite hors CLI pouvait porter un chemin, seulement REDIGE a
    l'affichage. Desormais elle est refusee a la mise en file (D-054): rien n'est ecrit.
    """
    from mervio.persistence.errors import PayloadRejected
    from mervio.persistence.tenancy import TenantContext, TenantSession
    session = TenantSession(db, TenantContext(uuid.UUID(world["a"]), user_id(evidence, ANALYST_A)))
    with pytest.raises(PayloadRejected):
        jobs.enqueue_job(session, job_type="analysis",
                         payload={"note": "/Users/alice/x.csv", "mail": "alice@example.com"})
    assert count(evidence, "SELECT count(*) FROM jobs") == 0


def test_job_records_are_rendered_without_emails(world, db, evidence):
    """Ce qui reste autorise dans une charge utile ne ressort toujours pas en clair."""
    from mervio.persistence.tenancy import TenantContext, TenantSession
    session = TenantSession(db, TenantContext(uuid.UUID(world["a"]), user_id(evidence, ANALYST_A)))
    job = jobs.enqueue_job(session, job_type="analysis", payload={"mail": "alice@example.com"})
    rendered = operations.render_job(job, detailed=True)
    assert rendered["payload"] == {"mail": "[redacted-email]"}
