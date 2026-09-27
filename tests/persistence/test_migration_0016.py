"""Revision `0016`: catalogue, aller-retour, et restauration LITTERALE de la politique de `0004`.

Ce que ce fichier defend, et que le test de schema existant ne couvrait pas: celui-ci ne
compare que les COLONNES. Une revision qui ajoute une politique, sept fonctions privilegiees,
un declencheur et trois domaines a besoin d'une empreinte plus large, sans quoi une descente
incomplete passerait inapercue.
"""
from __future__ import annotations

import psycopg
import pytest

from mervio.persistence import migrate

PREVIOUS = "0015_raw_object_purge"

PURGE_FUNCTIONS = ("app_close_organization", "app_close_store", "app_destroy_identity_key",
                   "app_purge_finalize_raw_object", "app_purge_tenant_data",
                   "app_tombstone_organization", "app_tombstone_store")

CLOSURE_GUARDED = ("stores", "connections", "data_snapshots", "snapshot_sources",
                   "analysis_runs", "reports", "raw_objects", "customer_redactions", "jobs")

#: empreinte RICHE: fonctions (corps compris), politiques (expression complete), contraintes,
#: declencheurs avec leurs arguments, colonnes, privileges de colonne, RLS.
FINGERPRINT = """
SELECT 'FN  '||p.proname||'('||pg_get_function_identity_arguments(p.oid)||') secdef='||p.prosecdef::text
       ||' cfg='||coalesce(array_to_string(p.proconfig,','),'-')||' acl='||coalesce(p.proacl::text,'DEFAULT')
       ||' md5='||md5(p.prosrc)
FROM pg_proc p WHERE p.pronamespace='public'::regnamespace
UNION ALL
SELECT 'POL '||c.relname||'.'||p.polname||' perm='||p.polpermissive::text||' cmd='||p.polcmd::text
       ||' roles='||coalesce((SELECT string_agg(r.rolname,',' ORDER BY r.rolname) FROM pg_roles r
                              WHERE r.oid=ANY(p.polroles)),'PUBLIC')
       ||' qual='||coalesce(pg_get_expr(p.polqual,p.polrelid),'-')
       ||' chk='||coalesce(pg_get_expr(p.polwithcheck,p.polrelid),'-')
FROM pg_policy p JOIN pg_class c ON c.oid=p.polrelid
UNION ALL
SELECT 'CON '||conrelid::regclass::text||'.'||conname||' '||pg_get_constraintdef(oid)
FROM pg_constraint WHERE connamespace='public'::regnamespace
UNION ALL
SELECT 'TRG '||c.relname||'.'||t.tgname||' type='||t.tgtype::text||' fn='||p.proname
       ||' args='||encode(t.tgargs,'escape')
FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_proc p ON p.oid=t.tgfoid
WHERE NOT t.tgisinternal
UNION ALL
SELECT 'COL '||table_name||'.'||column_name||' '||data_type||' null='||is_nullable
       ||' def='||coalesce(column_default,'-')
FROM information_schema.columns WHERE table_schema='public'
UNION ALL
SELECT 'GRT '||table_name||' '||grantee||' '||privilege_type||' '||coalesce(column_name,'*')
FROM information_schema.column_privileges
WHERE table_schema='public' AND grantee IN ('mervio_app','mervio_worker')
UNION ALL
SELECT 'RLS '||relname||' rls='||relrowsecurity::text||' force='||relforcerowsecurity::text
FROM pg_class WHERE relnamespace='public'::regnamespace AND relkind='r'
ORDER BY 1
"""

#: la clause de `0004`, telle que le catalogue la rend. Ecrite ici EN DUR: si la descente
#: devait un jour la reconstruire autrement, ce test doit echouer, pas s'adapter.
ORIGINAL_POLICY = ("((status = ANY (ARRAY['succeeded'::text, 'failed'::text, 'cancelled'::text])) "
                   "AND (finished_at IS NOT NULL) "
                   "AND (finished_at < (now() - '01:00:00'::interval)))")


@pytest.fixture
def staged(pg):
    """Une base neuve montee jusqu'a `0015`: le point de depart de `0016`."""
    name = pg.create_empty_database("m0016")
    url = pg.url("migrator", name)
    try:
        migrate.upgrade(url, PREVIOUS)
        yield url
    finally:
        pg.drop_database(name)


def _fingerprint(url):
    with psycopg.connect(url) as conn:
        return [r[0] for r in conn.execute(FINGERPRINT).fetchall()]


def _policy(url):
    with psycopg.connect(url) as conn:
        return conn.execute(
            "SELECT p.polpermissive, p.polcmd::text, "
            "  coalesce((SELECT string_agg(r.rolname,',') FROM pg_roles r WHERE r.oid=ANY(p.polroles)),'PUBLIC'), "
            "  pg_get_expr(p.polqual, p.polrelid) "
            "FROM pg_policy p WHERE p.polname = 'jobs_purge_terminal_only'").fetchone()


def test_0016_is_the_head_and_follows_0015():
    assert migrate.revisions()[-1] == "0016_tenant_purge"
    assert migrate.revisions()[-2] == PREVIOUS


def test_the_roundtrip_restores_the_catalogue(staged):
    """BASE -> UP -> DOWN -> UP: la montee est DETERMINISTE, la descente rend l'etat d'origine.

    Une seule difference subsiste entre BASE et DOWN, et elle est VOULUE: les trois domaines
    rendus a leur liste d'origine reviennent `NOT VALID`. C'est le mecanisme de `0008`, repris
    par `0013` et `0014`, dont les descentes font deja exactement cela -- une descente ne peut
    pas VALIDER une contrainte contre des lignes susceptibles de la violer. Le test l'affirme
    au lieu de la masquer: la difference est ENUMEREE, et rien d'autre ne doit differer.
    """
    base = _fingerprint(staged)
    # garde-fou: une base reellement montee jusqu'a 0015 porte plusieurs centaines de lignes
    # d'empreinte. Sans cela, un aller-retour sur une base vide passerait trivialement.
    assert len(base) > 500, f"la base de depart n'est pas migree: {len(base)} lignes"
    migrate.upgrade(staged)
    up1 = _fingerprint(staged)
    migrate.downgrade(staged)
    down = _fingerprint(staged)
    migrate.upgrade(staged)
    up2 = _fingerprint(staged)

    assert up1 == up2, "la montee doit etre deterministe"
    assert len(up1) > len(base), "0016 ajoute bien des objets"

    only_in_base = [line for line in base if line not in down]
    only_in_down = [line for line in down if line not in base]
    assert only_in_down == [line + " NOT VALID" for line in only_in_base]
    assert {line.split(".")[1].split(" ")[0] for line in only_in_base} == {
        "jobs_job_type_check", "raw_objects_purge_reason_known", "audit_events_action_check"}


def test_the_downgrade_restores_the_0004_policy_verbatim(staged):
    """Exigence DURE de D-066: la politique revient au caractere pres."""
    before = _policy(staged)
    assert before == (False, "d", "PUBLIC", ORIGINAL_POLICY)
    migrate.upgrade(staged)
    during = _policy(staged)
    assert during[3] != ORIGINAL_POLICY and "purging" in during[3]
    migrate.downgrade(staged)
    assert _policy(staged) == before


def test_the_d066_disjunction_covers_only_the_time_floor(staged):
    """La forme ELLE-MEME est verifiee: I1 et I2 restent EN FACTEUR de la disjonction.

    Si la disjonction remontait d'un cran, `queued` et `running` deviendraient supprimables
    pendant une purge -- et le travail de purge pourrait se detruire lui-meme.
    """
    migrate.upgrade(staged)
    qual = _policy(staged)[3]
    head, _, tail = qual.partition(" AND ((finished_at < ")
    assert tail, f"la disjonction doit s'ouvrir APRES les deux invariants: {qual}"
    assert "status = ANY" in head and "finished_at IS NOT NULL" in head
    assert "status = ANY" not in tail and "OR (EXISTS" in tail
    assert "'purging'" in tail


def test_the_purge_functions_and_the_guard_disappear_on_downgrade(staged):
    """Ordre de suppression: le moteur ne le protege PAS, la migration et ce test le portent.

    PostgreSQL n'enregistre aucune dependance `pg_depend` entre une fonction plpgsql et celles
    qu'elle appelle: retirer la garde avant ses appelants reussirait, en laissant sept
    fonctions privilegiees pointer dans le vide. Le test verifie donc l'ETAT FINAL, et
    l'absence de dependance enregistree -- pour que personne ne croie le moteur protecteur.
    """
    migrate.upgrade(staged)
    with psycopg.connect(staged) as conn:
        present = {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace").fetchall()}
        assert set(PURGE_FUNCTIONS) <= present and "app_purge_guard" in present
        recorded = conn.execute(
            "SELECT count(*) FROM pg_depend d "
            "JOIN pg_proc src ON src.oid = d.objid JOIN pg_proc tgt ON tgt.oid = d.refobjid "
            "WHERE src.proname = ANY(%s) AND tgt.proname = 'app_purge_guard'",
            (list(PURGE_FUNCTIONS),)).fetchone()[0]
        assert recorded == 0, "aucune dependance n'est enregistree: l'ordre est notre affaire"
    migrate.downgrade(staged)
    with psycopg.connect(staged) as conn:
        remaining = {r[0] for r in conn.execute(
            "SELECT proname FROM pg_proc WHERE pronamespace = 'public'::regnamespace").fetchall()}
        assert not (set(PURGE_FUNCTIONS) & remaining)
        assert "app_purge_guard" not in remaining
        assert "tenant_closure_guard" not in remaining
        assert {"app_redact_customer", "app_finalize_raw_object_purge"} <= remaining


def test_the_closure_trigger_covers_the_entry_points_and_not_the_canonical_tables(staged):
    """Les canoniques sont protegees TRANSITIVEMENT: pas de declencheur par ligne sur l'import."""
    migrate.upgrade(staged)
    with psycopg.connect(staged) as conn:
        guarded = {r[0] for r in conn.execute(
            "SELECT c.relname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND t.tgname LIKE '%_closure_guard'").fetchall()}
    assert guarded == set(CLOSURE_GUARDED)
    assert not (guarded & {"orders", "order_lines", "products", "payments", "refunds",
                           "campaigns", "ad_daily_performance"})
