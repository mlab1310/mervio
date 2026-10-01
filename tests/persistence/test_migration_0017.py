"""Revision `0017` (004.4.7, D-070): une contrainte, et rien d'autre.

Ce que ce fichier defend:
  - `0017` est la TETE et suit `0016`;
  - la montee n'ajoute QUE la valeur `organization.purge_recovery_refused` a
    `audit_events_action_check`, et ne touche AUCUN autre objet du catalogue;
  - la descente rend la liste de `0016` **au caractere pres**, en `NOT VALID`;
  - un evenement D-070 deja ecrit SURVIT a la descente -- le journal est en ajout seul, une
    migration n'en efface rien -- et la remontee le revalide;
  - l'enum Python `Action` couvre la contrainte, garde-fou que `0013` et `0014` portaient et
    que `0016` avait omis, laissant quatre actions ECRITES mais NON FILTRABLES par la CLI.
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from mervio.persistence import migrate
from mervio.persistence.audit import Action

PREVIOUS = "0016_tenant_purge"
HEAD = "0017_purge_recovery_audit"

REFUSAL = "organization.purge_recovery_refused"
#: les quatre actions de `0016`, qui n'etaient pas dans l'enum avant 004.4.7
TENANT_PURGE_ACTIONS = ("store.purge_started", "store.purged",
                        "organization.purge_started", "organization.purged")

CONSTRAINT = ("SELECT pg_get_constraintdef(oid), convalidated FROM pg_constraint "
              "WHERE conname = 'audit_events_action_check'")

#: empreinte du catalogue, assez large pour qu'un objet touche par megarde apparaisse.
FINGERPRINT = """
SELECT 'FN  '||p.proname||'('||pg_get_function_identity_arguments(p.oid)||') secdef='||p.prosecdef::text
       ||' cfg='||coalesce(array_to_string(p.proconfig,','),'-')||' acl='||coalesce(p.proacl::text,'DEFAULT')
       ||' md5='||md5(p.prosrc)
FROM pg_proc p WHERE p.pronamespace='public'::regnamespace
UNION ALL
SELECT 'POL '||c.relname||'.'||p.polname||' perm='||p.polpermissive::text||' cmd='||p.polcmd::text
       ||' qual='||coalesce(pg_get_expr(p.polqual,p.polrelid),'-')
       ||' chk='||coalesce(pg_get_expr(p.polwithcheck,p.polrelid),'-')
FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
UNION ALL
SELECT 'CON '||conrelid::regclass::text||'.'||conname||' '||pg_get_constraintdef(oid)
       ||' valid='||convalidated::text
FROM pg_constraint WHERE connamespace='public'::regnamespace
UNION ALL
SELECT 'TRG '||c.relname||'.'||t.tgname||' type='||t.tgtype::text||' fn='||p.proname
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_proc p ON p.oid=t.tgfoid
WHERE NOT t.tgisinternal
UNION ALL
SELECT 'COL '||table_name||'.'||column_name||' '||data_type||' null='||is_nullable
FROM information_schema.columns WHERE table_schema='public'
UNION ALL
SELECT 'GRT '||table_name||' '||grantee||' '||privilege_type||' '||coalesce(column_name,'*')
FROM information_schema.column_privileges
WHERE table_schema='public' AND grantee IN ('mervio_app','mervio_worker')
UNION ALL
SELECT 'IDX '||indexname||' '||indexdef FROM pg_indexes WHERE schemaname='public'
UNION ALL
SELECT 'RLS '||relname||' rls='||relrowsecurity::text||' force='||relforcerowsecurity::text
FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'
ORDER BY 1
"""


@pytest.fixture
def staged(pg):
    """Une base neuve montee jusqu'a `0016`: le point de depart de `0017`."""
    name = pg.create_empty_database("m0017")
    url = pg.url("migrator", name)
    try:
        migrate.upgrade(url, PREVIOUS)
        yield url
    finally:
        pg.drop_database(name)


def _fingerprint(url):
    with psycopg.connect(url) as conn:
        return [r[0] for r in conn.execute(FINGERPRINT).fetchall()]


def _constraint(url):
    with psycopg.connect(url) as conn:
        return conn.execute(CONSTRAINT).fetchone()


def _seed_refusal(url) -> uuid.UUID:
    """Ecrit un evenement D-070, par le proprietaire du schema, sur une organisation minimale.

    Volontairement en SQL direct: ce test porte sur la MIGRATION, pas sur le chemin applicatif.
    """
    organization_id, user_id, event_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with psycopg.connect(url) as conn:
        # `users` est la seule table sans RLS (`0001`); `organizations` et `audit_events` sont en
        # FORCE ROW LEVEL SECURITY, a laquelle le PROPRIETAIRE du schema est lui aussi soumis --
        # d'ou le contexte de tenant, sans lequel les deux `WITH CHECK` refuseraient.
        conn.execute("INSERT INTO users (id, idp_subject, kind) VALUES (%s, %s, 'human')",
                     (user_id, f"seed|{user_id}"))
        conn.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
        conn.execute("INSERT INTO organizations (id, name) VALUES (%s, %s)",
                     (organization_id, f"seed-{organization_id}"))
        conn.execute(
            "INSERT INTO audit_events (id, organization_id, actor_type, actor_id, action, "
            "resource_type, resource_id, correlation_id, outcome) "
            "VALUES (%s, %s, 'user', %s, %s, 'organization', %s, %s, 'failed')",
            (event_id, organization_id, user_id, REFUSAL, organization_id, uuid.uuid4()))
        conn.commit()
    return organization_id, event_id


def _count(url, organization_id, event_id) -> int:
    """`audit_events` est sous RLS forcee: la lecture exige le contexte de l'organisation."""
    with psycopg.connect(url) as conn:
        conn.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
        return conn.execute("SELECT count(*) FROM audit_events WHERE id = %s",
                            (event_id,)).fetchone()[0]


# =============================================================================================
# 1. place dans la chaine
# =============================================================================================

def test_0017_is_the_head_and_follows_0016():
    assert migrate.revisions()[-1] == HEAD
    assert migrate.revisions()[-2] == PREVIOUS


# =============================================================================================
# 2. la contrainte, et elle seule
# =============================================================================================

def test_the_upgrade_adds_exactly_one_value(staged):
    before, before_valid = _constraint(staged)
    assert REFUSAL not in before and before_valid is True
    migrate.upgrade(staged)
    after, after_valid = _constraint(staged)
    assert REFUSAL in after and after_valid is True
    # la liste s'allonge d'UNE valeur: aucune autre n'est ajoutee, aucune n'est retiree
    assert after.count("'") == before.count("'") + 2


def test_the_upgrade_touches_nothing_but_that_constraint(staged):
    """Perimetre DB strict: une seule ligne d'empreinte bouge, et c'est la contrainte."""
    base = _fingerprint(staged)
    assert len(base) > 500, f"la base de depart n'est pas migree: {len(base)} lignes"
    migrate.upgrade(staged)
    after = _fingerprint(staged)

    only_before = [line for line in base if line not in after]
    only_after = [line for line in after if line not in base]
    assert len(only_before) == 1 and len(only_after) == 1, (only_before, only_after)
    assert "audit_events.audit_events_action_check" in only_before[0]
    assert "audit_events.audit_events_action_check" in only_after[0]
    assert REFUSAL in only_after[0] and REFUSAL not in only_before[0]


def test_the_refusal_action_is_rejected_before_and_accepted_after(staged):
    with pytest.raises(psycopg.errors.CheckViolation):
        _seed_refusal(staged)
    migrate.upgrade(staged)
    assert _count(staged, *_seed_refusal(staged)) == 1


# =============================================================================================
# 3. descente
# =============================================================================================

def test_the_downgrade_restores_the_0016_list_not_valid(staged):
    """La liste de `0016` revient au caractere pres, et en `NOT VALID` -- mecanisme de `0008`."""
    before, before_valid = _constraint(staged)
    migrate.upgrade(staged)
    migrate.downgrade(staged)
    after, after_valid = _constraint(staged)
    # `pg_get_constraintdef` suffixe " NOT VALID" quand la contrainte ne l'est pas: la
    # comparaison porte sur la CONDITION, et la validite est lue separement.
    assert after.removesuffix(" NOT VALID") == before, "la liste de 0016 doit revenir au caractere pres"
    assert after.endswith(" NOT VALID")
    assert before_valid is True and after_valid is False, "la descente laisse NOT VALID"


def test_the_downgrade_refuses_new_refusals(staged):
    migrate.upgrade(staged)
    migrate.downgrade(staged)
    with pytest.raises(psycopg.errors.CheckViolation):
        _seed_refusal(staged)


def test_an_existing_refusal_survives_the_downgrade(staged):
    """Le journal est en AJOUT SEUL: une migration n'en efface rien. C'est tout le sens de
    `NOT VALID` -- la descente ne POURRAIT pas valider la contrainte contre cette ligne."""
    migrate.upgrade(staged)
    organization_id, event = _seed_refusal(staged)
    migrate.downgrade(staged)
    assert _count(staged, organization_id, event) == 1, "la descente a detruit une trace d'audit"


def test_the_upgrade_works_again_with_refusals_already_present(staged):
    """UP -> DOWN -> UP avec un evenement D-070 en base: la remontee REVALIDE la liste."""
    migrate.upgrade(staged)
    organization_id, event = _seed_refusal(staged)
    migrate.downgrade(staged)
    migrate.upgrade(staged)
    definition, valid = _constraint(staged)
    assert REFUSAL in definition and valid is True
    assert _count(staged, organization_id, event) == 1
    # et la base accepte de nouveau de NOUVELLES traces
    assert _count(staged, *_seed_refusal(staged)) == 1


def test_the_roundtrip_is_deterministic(staged):
    """UP1 == UP2, et la seule difference BASE/DOWN est la contrainte rendue `NOT VALID`."""
    base = _fingerprint(staged)
    migrate.upgrade(staged)
    up1 = _fingerprint(staged)
    migrate.downgrade(staged)
    down = _fingerprint(staged)
    migrate.upgrade(staged)
    up2 = _fingerprint(staged)

    assert up1 == up2, "la montee doit etre deterministe"
    only_in_base = [line for line in base if line not in down]
    only_in_down = [line for line in down if line not in base]
    # UNE seule ligne differe, et c'est la contrainte. Elle differe en DEUX endroits, tous
    # deux voulus: le suffixe ` NOT VALID` de `pg_get_constraintdef` et le drapeau `valid=`.
    assert len(only_in_base) == 1 and len(only_in_down) == 1, (only_in_base, only_in_down)
    assert "audit_events_action_check" in only_in_base[0]
    assert only_in_base[0].endswith(" valid=true")
    assert only_in_down[0].endswith(" NOT VALID valid=false")
    assert (only_in_down[0].removesuffix(" NOT VALID valid=false")
            == only_in_base[0].removesuffix(" valid=true")), "la condition doit etre identique"


# =============================================================================================
# 4. l'enum Python couvre la contrainte -- garde-fou que `0016` avait omis
# =============================================================================================

def test_the_action_enum_covers_the_refusal_action():
    assert REFUSAL in {a.value for a in Action}


def test_the_action_enum_covers_the_tenant_purge_actions_of_0016():
    """Dette de `0016`, fermee ici. `0013` et `0014` portaient ce garde-fou; `0016` l'a omis,
    et quatre actions etaient donc ECRITES en base mais refusees par `audit list --action`."""
    assert {a.value for a in Action} >= set(TENANT_PURGE_ACTIONS)


def test_the_enum_and_the_constraint_agree_exactly(staged):
    """Ni plus, ni moins: une valeur dans l'enum sans contrainte serait insérable nulle part,
    une valeur en base sans enum serait ecrite et non filtrable -- le defaut de `0016`."""
    migrate.upgrade(staged)
    definition, _ = _constraint(staged)
    in_db = {part.strip().strip("'") for part in
             definition.split("ARRAY[", 1)[1].rsplit("]", 1)[0].split(",")}
    in_db = {value.replace("::text", "").strip("'") for value in in_db}
    assert in_db == {a.value for a in Action}
