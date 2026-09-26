"""Resolution identite -> `customer_ref`, locale au worker (004.4.5 E6, D-062). PostgreSQL.

Le chainon qui manquait a l'effacement client. Ce que ces tests prouvent EN BASE, sous les vrais
roles:

- la reference resolue est EXACTEMENT celle que l'import a persistee -- sans quoi l'effacement
  porterait sur un client qui n'existe pas;
- elle est deterministe, et liee a l'organisation: la meme identite donne deux references
  differentes dans deux organisations, parce que leurs sels different;
- la resolution est OBSERVATIONNELLEMENT EN LECTURE SEULE: l'etat de la base est identique avant
  et apres, et surtout un sel absent n'est PAS cree;
- les refus (sel absent, sel detruit, autre cle maitre, service non autorise, delegue absent ou de
  rang insuffisant) ne distinguent rien qui ferait un oracle, et ne citent aucune valeur;
- de bout en bout: identite -> resolution -> `enqueue-redact` -> effacement -> tombstone, l'identite
  n'apparaissant ni en charge utile, ni en audit, ni en log, ni en base.

Le cadrage de l'entree et le contrat de stdout sont couverts sans base par
tests/test_resolve_customer_ref.py.
"""
from __future__ import annotations

import re
import uuid

import pytest

from mervio.admin import operations
from mervio.application.persisted_analysis import SnapshotImportRequest, import_csv_snapshot
from mervio.identity import REF_PATTERN, MasterKey
from mervio.persistence import jobs, snapshots
from mervio.persistence.errors import IdentityKeyUnavailable, PermissionDenied
from mervio.persistence.identity_keys import ensure_identity_key, load_identity_key
from mervio.persistence.jobs import JobType
from mervio.persistence.service import ServiceSession, authorize_service, delegated_session
from mervio.persistence.tenancy import (Permission, Role, add_member,
                                       ensure_user)
from mervio.settings import AdminSettings, ResolutionSettings, SettingsError
from mervio.storage import MemoryObjectStore
from mervio.workers import resolution
from mervio.workers.handlers import JobContext, make_redact_customer_handler

from .persistence_support import SAMPLE_FILES, TEST_MASTER_KEY, TEST_MASTER_KEY_HEX

#: Identite presente dans les CSV synthetiques d'exemple: c'est elle que l'import a hachee.
KNOWN_IDENTITY = "emma.garcia18@example.com"
#: Une autre cle maitre, au format valide: elle ne doit JAMAIS servir de cle de substitution (F-03).
OTHER_MASTER_HEX = "5c" * 32
TOMBSTONE = re.compile(r"^redacted:[0-9a-f-]{36}$")
#: Tables de tenant, dans l'ordre de `conftest.TABLES`: l'empreinte de l'etat les couvre toutes.
SNAPSHOT_TABLES = ("service_authorizations", "audit_events", "customer_redactions", "jobs", "raw_objects",
                   "reports", "analysis_runs", "ad_daily_performance", "campaigns", "refunds", "payments",
                   "order_lines", "orders", "products", "snapshot_sources", "data_snapshots",
                   "connections", "stores", "memberships", "users", "organization_identity_keys",
                   "organizations")


# -- mise en place ------------------------------------------------------------------------------

@pytest.fixture
def resolvable(db, tenant_a, worker_db, principal):
    """Une organisation AUTORISEE pour le service worker, dont le sel existe deja par un import."""
    authorize_service(tenant_a.session, principal.id)
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    return tenant_a


def _resolve(database, tenant, identity=KNOWN_IDENTITY, *, actor, master=TEST_MASTER_KEY):
    """Resolution telle que la commande l'execute: connexion WORKER, delegue humain NOMME."""
    return resolution.resolve_customer_ref(
        database, organization_id=tenant.organization_id, actor_subject=actor,
        identity=identity, master=master)


def _owner_subject(db, tenant) -> str:
    with db.transaction() as conn:
        return conn.execute("SELECT idp_subject FROM users WHERE id = %s", (tenant.owner_id,)).fetchone()[0]


def _digest(owner, organization_id) -> dict:
    """Empreinte de TOUT l'etat visible de l'organisation: une ligne de plus ou de moins la change."""
    state = {}
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        for table in SNAPSHOT_TABLES:
            state[table] = owner.execute(
                f"SELECT count(*), coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') "
                f"FROM {table} t").fetchone()
    return state


def _import(tenant, *, master=TEST_MASTER_KEY):
    """Import reel: c'est LUI qui fixe les references que la resolution doit retrouver.

    Meme appel que tests/persistence/test_identity_pii.py: la pile d'ingestion recoit des chemins
    LOCAUX (le worker, lui, materialise d'abord les octets d'un `raw_object`, D-061). Ce que E6
    doit prouver est l'egalite des REFERENCES, pas le chemin des octets.
    """
    return import_csv_snapshot(tenant.session, store_id=tenant.store_id, connection_id=tenant.connection_id,
                               request=SnapshotImportRequest(**SAMPLE_FILES), master_key=master)


# -- LA propriete: la reference resolue est celle que l'import a persistee -----------------------

def test_the_resolved_reference_is_exactly_the_one_the_import_persisted(db, tenant_a, worker_db, principal):
    """Sans cette egalite, tout E6 serait inutile: l'effacement porterait sur un client inexistant."""
    authorize_service(tenant_a.session, principal.id)
    result = _import(tenant_a)
    persisted = {order.customer_id for order in
                 snapshots.load_dataset(tenant_a.session, tenant_a.store_id, result.snapshot.id).orders}

    reference = _resolve(worker_db, tenant_a, actor=_owner_subject(db, tenant_a))

    assert REF_PATTERN.fullmatch(reference) and reference.startswith("c1:")
    assert reference in persisted, "la reference resolue n'est pas celle de l'import"


def test_the_case_and_the_surrounding_spaces_do_not_change_the_reference(db, tenant_a, worker_db, principal):
    """Meme normalisation que les connecteurs: espaces retires, minuscules (D-053)."""
    authorize_service(tenant_a.session, principal.id)
    _import(tenant_a)
    subject = _owner_subject(db, tenant_a)
    canonical = _resolve(worker_db, tenant_a, KNOWN_IDENTITY, actor=subject)
    for variant in (KNOWN_IDENTITY.upper(), f"  {KNOWN_IDENTITY}  ", KNOWN_IDENTITY.capitalize()):
        assert _resolve(worker_db, tenant_a, variant, actor=subject) == canonical


# -- determinisme et absence d'alea -------------------------------------------------------------

def test_repeated_resolutions_return_exactly_the_same_reference(db, resolvable, worker_db):
    """E6 n'utilise pas `uuid4`: la reference est un HMAC, donc stable a l'identique."""
    subject = _owner_subject(db, resolvable)
    references = {_resolve(worker_db, resolvable, actor=subject) for _ in range(10)}
    assert len(references) == 1
    assert REF_PATTERN.fullmatch(references.pop())


def test_a_fresh_connection_and_a_fresh_key_object_give_the_same_reference(db, resolvable, worker_db, pg):
    """Determinisme reel: rien n'est memoise entre deux processus, seul le sel et la cle maitre comptent."""
    from mervio.persistence.database import Database
    subject = _owner_subject(db, resolvable)
    first = _resolve(worker_db, resolvable, actor=subject)
    other = Database(pg.url("worker"))
    try:
        second = _resolve(other, resolvable, actor=subject,
                          master=MasterKey.from_hex(TEST_MASTER_KEY_HEX))
    finally:
        other.close()
    assert first == second


# -- liaison a l'organisation -------------------------------------------------------------------

def test_the_same_identity_gives_different_references_in_two_organizations(db, tenant_a, tenant_b,
                                                                          worker_db, principal):
    """Les sels differents rendent les references incomparables d'une organisation a l'autre (D-053)."""
    for tenant in (tenant_a, tenant_b):
        authorize_service(tenant.session, principal.id)
        ensure_identity_key(tenant.session, TEST_MASTER_KEY)
    in_a = _resolve(worker_db, tenant_a, actor=_owner_subject(db, tenant_a))
    in_b = _resolve(worker_db, tenant_b, actor=_owner_subject(db, tenant_b))
    assert in_a != in_b
    assert in_a.startswith("c1:") and in_b.startswith("c1:")


def test_a_reference_from_one_organization_erases_nothing_in_another(db, owner, tenant_a, tenant_b,
                                                                     worker_db, principal):
    """Une reference d'A n'est pas une preuve d'identite dans B: elle n'y designe personne."""
    for tenant in (tenant_a, tenant_b):
        authorize_service(tenant.session, principal.id)
    _import(tenant_a)
    _import(tenant_b)
    in_a = _resolve(worker_db, tenant_a, actor=_owner_subject(db, tenant_a))

    refs_b = [row[0] for row in _rows(owner, tenant_b.organization_id, "SELECT customer_ref FROM orders")]
    assert in_a not in refs_b

    # mise en file dans B avec la reference d'A: le travail passe, mais n'efface rien
    job = jobs.enqueue_job(tenant_b.session, job_type=JobType.REDACT_CUSTOMER, payload={"customer_ref": in_a})
    outcome = _run_erasure(worker_db, principal, tenant_b, job)
    assert outcome["orders_tombstoned"] == 0
    assert [row[0] for row in _rows(owner, tenant_b.organization_id,
                                    "SELECT customer_ref FROM orders")] == refs_b


def _rows(owner, organization_id, query):
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)", (str(organization_id),))
        return owner.execute(query).fetchall()


def _run_erasure(worker_db, principal, tenant, job, *, store=None, log=None):
    claimed = jobs.claim_next_job(tenant.session, worker_id="worker-1", job_id=job.id)
    assert claimed is not None
    session = delegated_session(ServiceSession(worker_db, tenant.organization_id, principal), claimed)
    handler = make_redact_customer_handler(store if store is not None else MemoryObjectStore())
    return handler(JobContext(session=session, job=claimed, correlation_id=str(claimed.correlation_id),
                              log=log or _Log(), actor_id=principal.id, on_behalf_of=claimed.enqueued_by))


class _Log:
    def __init__(self):
        self.events = []

    def info(self, name, **fields):
        self.events.append((name, fields))

    warning = error = debug = info

    def bind(self, **fields):
        return self


# -- LECTURE SEULE: aucune mutation, et surtout aucun sel cree ----------------------------------

def test_a_successful_resolution_leaves_the_database_bit_for_bit_identical(db, owner, resolvable, worker_db):
    """Le test d'effet de bord exige par la tranche: etat = X avant, etat = X apres."""
    subject = _owner_subject(db, resolvable)
    before = _digest(owner, resolvable.organization_id)
    assert _resolve(worker_db, resolvable, actor=subject)
    assert _digest(owner, resolvable.organization_id) == before


def test_resolving_an_unknown_identity_writes_nothing_either(db, owner, resolvable, worker_db):
    """Une identite qui n'a jamais ete importee ne cree aucune ligne: la resolution ne consulte
    AUCUNE donnee client, donc elle ne peut meme pas savoir que celle-la est inconnue."""
    subject = _owner_subject(db, resolvable)
    before = _digest(owner, resolvable.organization_id)
    reference = _resolve(worker_db, resolvable, "jamais-importe@example.invalid", actor=subject)
    assert REF_PATTERN.fullmatch(reference)
    assert _digest(owner, resolvable.organization_id) == before


def test_a_missing_salt_is_refused_and_is_NOT_created(db, owner, tenant_a, worker_db, principal):
    """LE piege de la tranche: `ensure_identity_key` creerait le sel et exigerait IMPORT_DATA.
    Une organisation sans sel n'a jamais produit de reference, donc n'a rien a effacer: refus."""
    authorize_service(tenant_a.session, principal.id)
    assert _rows(owner, tenant_a.organization_id,
                 "SELECT count(*) FROM organization_identity_keys")[0][0] == 0
    before = _digest(owner, tenant_a.organization_id)

    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, tenant_a, actor=_owner_subject(db, tenant_a))

    assert refused.value.code == "not_visible"
    assert _rows(owner, tenant_a.organization_id,
                 "SELECT count(*) FROM organization_identity_keys")[0][0] == 0
    assert _digest(owner, tenant_a.organization_id) == before


def test_the_resolution_never_calls_the_creating_path(db, resolvable, worker_db, monkeypatch):
    """Garde comportementale: `ensure_identity_key` est rendue explosive, et la resolution reussit
    quand meme -- elle ne passe donc pas par elle. Le pendant STATIQUE (le nom n'apparait nulle
    part dans le module) est dans tests/test_resolve_customer_ref.py."""
    import mervio.persistence.identity_keys as module

    def forbidden(*args, **kwargs):
        raise AssertionError("la resolution ne doit jamais appeler ensure_identity_key")

    monkeypatch.setattr(module, "ensure_identity_key", forbidden)
    assert REF_PATTERN.fullmatch(_resolve(worker_db, resolvable, actor=_owner_subject(db, resolvable)))
    # la garde mord bien: le chemin d'import, lui, l'appelle
    with pytest.raises(AssertionError):
        module.ensure_identity_key(resolvable.session, TEST_MASTER_KEY)


def test_a_refused_resolution_writes_no_audit_event(db, owner, resolvable, worker_db):
    """D-062 n'adopte PAS E' : la resolution n'ecrit aucun evenement d'audit, reussie ou non."""
    subject = _owner_subject(db, resolvable)
    before = _rows(owner, resolvable.organization_id, "SELECT count(*) FROM audit_events")[0][0]
    assert _resolve(worker_db, resolvable, actor=subject)
    with pytest.raises(resolution.ResolutionRefused):
        _resolve(worker_db, resolvable, actor="test|inconnu|jamais-cree")
    assert _rows(owner, resolvable.organization_id,
                 "SELECT count(*) FROM audit_events")[0][0] == before


# -- refus: aucun oracle, aucune valeur ---------------------------------------------------------

def test_a_destroyed_salt_is_refused(db, owner, resolvable, worker_db):
    """Sel detruit (D-051): la cle d'organisation est irrecuperable, meme avec la cle maitre."""
    with owner.transaction():
        owner.execute("SELECT set_config('app.organization_id', %s, true)",
                      (str(resolvable.organization_id),))
        owner.execute("UPDATE organization_identity_keys SET salt = NULL, destroyed_at = now() "
                      "WHERE organization_id = %s", (resolvable.organization_id,))
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor=_owner_subject(db, resolvable))
    assert refused.value.code == "destroyed"


def test_another_master_key_is_refused_and_never_substituted(db, resolvable, worker_db):
    """F-03: la cle maitre de la premiere utilisation est la reference definitive."""
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor=_owner_subject(db, resolvable),
                 master=MasterKey.from_hex(OTHER_MASTER_HEX))
    assert refused.value.code == "master_key_mismatch"


def test_a_wrong_master_key_yields_no_useful_resolution(db, tenant_a, worker_db, principal):
    """Meme quand la cle maitre CREE le sel, une autre cle ne retrouve pas la meme reference."""
    authorize_service(tenant_a.session, principal.id)
    ensure_identity_key(tenant_a.session, MasterKey.from_hex(OTHER_MASTER_HEX))
    subject = _owner_subject(db, tenant_a)
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, tenant_a, actor=subject, master=TEST_MASTER_KEY)
    assert refused.value.code == "master_key_mismatch"
    # avec la bonne cle, la reference existe -- mais elle differe de celle de l'autre cle
    with_other = _resolve(worker_db, tenant_a, actor=subject, master=MasterKey.from_hex(OTHER_MASTER_HEX))
    assert REF_PATTERN.fullmatch(with_other)


def test_a_missing_master_key_is_refused(db, resolvable, worker_db):
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor=_owner_subject(db, resolvable), master=None)
    assert refused.value.code == "identity_master_key_missing"


def test_an_unauthorized_service_cannot_resolve_and_looks_like_a_missing_organization(db, tenant_a, worker_db):
    """Aucune autorisation de service: indiscernable d'une organisation inexistante (D-050, D-062)."""
    ensure_identity_key(tenant_a.session, TEST_MASTER_KEY)
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, tenant_a, actor=_owner_subject(db, tenant_a))
    assert refused.value.code == "not_visible"


def test_an_unknown_organization_gives_the_very_same_refusal(db, resolvable, worker_db):
    with pytest.raises(resolution.ResolutionRefused) as refused:
        resolution.resolve_customer_ref(worker_db, organization_id=uuid.uuid4(),
                                        actor_subject=_owner_subject(db, resolvable),
                                        identity=KNOWN_IDENTITY, master=TEST_MASTER_KEY)
    assert refused.value.code == "not_visible"


def test_an_unknown_actor_is_refused_and_is_not_created(db, owner, resolvable, worker_db):
    """Comme `mervio admin --as`: une faute de frappe ne provisionne personne."""
    before = _rows(owner, resolvable.organization_id, "SELECT count(*) FROM users")[0][0]
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor="test|jamais|vu")
    assert refused.value.code == "actor_unknown"
    assert _rows(owner, resolvable.organization_id, "SELECT count(*) FROM users")[0][0] == before


def test_a_service_principal_cannot_stand_in_for_the_human_delegate(db, resolvable, worker_db, principal):
    """Le sel n'est ouvert qu'a un delegue HUMAIN (`app_delegate_rank`), jamais au service lui-meme."""
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor=principal.subject)
    assert refused.value.code == "actor_unknown"


def test_a_human_who_is_not_a_member_is_refused(db, resolvable, worker_db):
    ensure_user(db, "test|resolution|etranger")
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor="test|resolution|etranger")
    assert refused.value.code == "not_visible"


def test_a_viewer_delegate_sees_no_salt_so_the_refusal_is_indistinguishable(db, resolvable, worker_db):
    """Plancher REEL de la revision 0011: `analyst` au moins. Un `viewer` ne voit aucune ligne, donc
    le refus est le meme que pour un sel absent -- aucun oracle n'est ajoute."""
    viewer = ensure_user(db, "test|resolution|viewer")
    add_member(resolvable.session, user_id=viewer, role=Role.VIEWER)
    with pytest.raises(resolution.ResolutionRefused) as refused:
        _resolve(worker_db, resolvable, actor="test|resolution|viewer")
    assert refused.value.code == "not_visible"


def test_an_analyst_delegate_is_enough(db, resolvable, worker_db):
    analyst = ensure_user(db, "test|resolution|analyst")
    add_member(resolvable.session, user_id=analyst, role=Role.ANALYST)
    assert REF_PATTERN.fullmatch(_resolve(worker_db, resolvable, actor="test|resolution|analyst"))


def test_no_refusal_message_ever_carries_the_identity(db, resolvable, worker_db):
    """D-016: aucun message ne porte de valeur. Surtout pas « client X introuvable »."""
    cases = [("test|jamais|vu", TEST_MASTER_KEY), (_owner_subject(db, resolvable), None),
             (_owner_subject(db, resolvable), MasterKey.from_hex(OTHER_MASTER_HEX))]
    for actor, master in cases:
        with pytest.raises(resolution.ResolutionRefused) as refused:
            _resolve(worker_db, resolvable, KNOWN_IDENTITY, actor=actor, master=master)
        text = str(refused.value) + refused.value.code
        assert KNOWN_IDENTITY not in text and "emma" not in text
        assert TEST_MASTER_KEY_HEX not in text and OTHER_MASTER_HEX not in text


# -- lecture stricte de la cle: la fonction elle-meme -------------------------------------------

def test_load_identity_key_requires_only_read_and_creates_nothing(db, owner, resolvable):
    """`load_identity_key` est la seule porte de la resolution: un SELECT, rien d'autre."""
    before = _digest(owner, resolvable.organization_id)
    key = load_identity_key(resolvable.session, TEST_MASTER_KEY)
    assert key.origin == "organization" and key.organization_id == resolvable.organization_id
    assert _digest(owner, resolvable.organization_id) == before


def test_load_identity_key_refuses_a_missing_row_instead_of_creating_it(db, owner, tenant_b):
    with pytest.raises(IdentityKeyUnavailable) as unavailable:
        load_identity_key(tenant_b.session, TEST_MASTER_KEY)
    assert unavailable.value.reason == "not_visible"
    assert _rows(owner, tenant_b.organization_id,
                 "SELECT count(*) FROM organization_identity_keys")[0][0] == 0


def test_load_identity_key_and_ensure_identity_key_agree_on_the_same_salt(db, resolvable):
    assert (load_identity_key(resolvable.session, TEST_MASTER_KEY).key_id
            == ensure_identity_key(resolvable.session, TEST_MASTER_KEY).key_id)


def test_a_bare_service_session_cannot_read_the_salt_at_all(db, resolvable, worker_db, principal):
    """Pourquoi la commande exige `--as`: sans delegue humain, la revision 0011 n'ouvre RIEN, et
    `load_identity_key` refuse donc au lieu de deriver une cle."""
    service = ServiceSession(worker_db, resolvable.organization_id, principal)
    with service.transaction(Permission.READ) as conn:
        assert conn.execute("SELECT count(*) FROM public.organization_identity_keys").fetchone()[0] == 0
    with pytest.raises(IdentityKeyUnavailable):
        load_identity_key(service, TEST_MASTER_KEY)


def test_a_service_session_cannot_even_ask_for_the_import_permission(db, resolvable, worker_db, principal):
    service = ServiceSession(worker_db, resolvable.organization_id, principal)
    with pytest.raises(PermissionDenied):
        with service.transaction(Permission.IMPORT_DATA):
            pass


# -- frontiere des secrets: `admin` ne resout rien ----------------------------------------------

def test_the_admin_settings_carry_no_master_key_even_when_the_variable_is_set():
    """D-062: `admin` ne voit la cle maitre a aucun moment, meme si l'environnement la porte."""
    settings = AdminSettings.from_env({"MERVIO_DATABASE_URL": "postgresql://u@127.0.0.1:5432/x",
                                       "MERVIO_IDENTITY_MASTER_KEY": TEST_MASTER_KEY_HEX,
                                       "MERVIO_OBJECT_STORE_ROOT": "/tmp/mervio-objects"})
    assert not hasattr(settings, "identity_master_key")
    assert TEST_MASTER_KEY_HEX not in repr(settings)
    fields = set(vars(settings))
    assert fields == {"database_url", "object_store"}


def test_the_resolution_settings_require_the_master_key_that_admin_never_gets():
    with pytest.raises(SettingsError) as refused:
        ResolutionSettings.from_env({"MERVIO_DATABASE_URL": "postgresql://u@127.0.0.1:5432/x"})
    assert [name for name, _ in refused.value.problems] == ["MERVIO_IDENTITY_MASTER_KEY"]
    settings = ResolutionSettings.from_env({"MERVIO_DATABASE_URL": "postgresql://u@127.0.0.1:5432/x",
                                            "MERVIO_IDENTITY_MASTER_KEY": TEST_MASTER_KEY_HEX})
    assert settings.identity_master_key.reveal() == TEST_MASTER_KEY_HEX


def test_the_admin_enqueue_refuses_an_identity_and_accepts_only_a_reference(db, resolvable, worker_db):
    """`admin` n'accepte AUCUNE identite directe: la frontiere est dans la validation, et en base."""
    subject = _owner_subject(db, resolvable)
    for rejected in (KNOWN_IDENTITY, "c1:" + "A" * 32, "c1:short", "redacted:" + str(uuid.uuid4()),
                     "c1:" + "z" * 32, "", None, 42):
        with pytest.raises(operations.InvalidInput):
            operations.validate_customer_ref(rejected)
    reference = _resolve(worker_db, resolvable, actor=subject)
    assert operations.validate_customer_ref(reference) == reference


# -- de bout en bout ---------------------------------------------------------------------------

def test_the_whole_operator_path_erases_the_right_customer_without_any_pii(db, owner, tenant_a,
                                                                          worker_db, principal):
    """identite -> resolution -> enqueue-redact -> effacement -> tombstone, sans PII nulle part."""
    authorize_service(tenant_a.session, principal.id)
    _import(tenant_a)
    subject = _owner_subject(db, tenant_a)
    before = [row[0] for row in _rows(owner, tenant_a.organization_id,
                                      "SELECT customer_ref FROM orders ORDER BY position")]

    # 1. le worker resout, localement, hors file
    reference = _resolve(worker_db, tenant_a, actor=subject)
    assert reference in before

    # 2. l'operateur met en file avec la seule reference
    enqueued = operations.enqueue_redact(db, actor=subject, organization=str(tenant_a.organization_id),
                                         customer_ref=reference)
    job_id = uuid.UUID(enqueued["job"]["id"])
    payload = _rows(owner, tenant_a.organization_id,
                    f"SELECT payload::text FROM jobs WHERE id = '{job_id}'")[0][0]
    assert payload == '{"customer_ref": "%s"}' % reference
    assert KNOWN_IDENTITY not in payload

    # 3. le worker efface
    log = _Log()
    job = jobs.get_job(tenant_a.session, job_id)
    outcome = _run_erasure(worker_db, principal, tenant_a, job, log=log)
    assert outcome["orders_tombstoned"] == before.count(reference) >= 1

    # 4. tombstone, et aucune trace de l'identite
    after = [row[0] for row in _rows(owner, tenant_a.organization_id,
                                     "SELECT customer_ref FROM orders ORDER BY position")]
    assert reference not in after
    tombstones = {ref for ref in after if ref not in before}
    # UN tombstone par client efface (D-056): une reanalyse donne des comptes par client identiques
    assert len(tombstones) == 1 and TOMBSTONE.fullmatch(next(iter(tombstones)))
    assert [a == b for a, b in zip(after, before)].count(False) == outcome["orders_tombstoned"]

    proofs = _rows(owner, tenant_a.organization_id,
                   "SELECT customer_ref, redacted_ref FROM customer_redactions")
    assert proofs == [(reference, next(iter(tombstones)))]

    everything = " ".join(str(row) for table in SNAPSHOT_TABLES
                          for row in _rows(owner, tenant_a.organization_id, f"SELECT t::text FROM {table} t"))
    assert KNOWN_IDENTITY not in everything and "emma.garcia18" not in everything
    assert KNOWN_IDENTITY not in str(log.events) and reference not in str(log.events)
