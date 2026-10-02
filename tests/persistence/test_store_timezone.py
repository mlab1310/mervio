"""AN-06 / ND-01 = B: plomberie reelle du fuseau de la boutique.

Les colonnes `stores.timezone` et `stores.timezone_source` existent depuis
`0011_identity_pii_schema` (l. 92-96) et sont enumerees par `0016_tenant_purge` (l. 170). Mais
AUCUN code applicatif ne les lisait ni ne les ecrivait: absentes de la dataclass `Store`,
absentes de `_STORE_COLUMNS`, jamais renseignees par `create_store()`. La roadmap et le handoff
004.4 les declaraient pourtant << livrees >> -- enonces rectifies en 004.5-A.

Ce module teste la chaine demandee par le GO:

    base  ->  Store  ->  _STORE_COLUMNS  ->  create / retrieve  ->  analytique

AUCUNE migration nouvelle n'est creee: les colonnes sont deja la, avec les bonnes valeurs par
defaut. C'etait la condition de ND-01 = B.

Point important sur la validation. Le `CHECK` de la migration 0011 est purement SYNTAXIQUE:
`timezone ~ '^[A-Za-z0-9_+./-]+$'` accepte `aaa/bbb`. La base ne peut pas valider un fuseau,
elle n'a pas de base de fuseaux applicative. La validation semantique est donc faite par
l'application AVANT l'ecriture, et ce module le prouve des deux cotes.
"""
from __future__ import annotations

import pytest

from mervio.errors import ConfigurationError
from mervio.persistence.stores import create_store, get_store


# -- la base porte bien les colonnes et leurs defauts ----------------------
def test_the_migration_already_provides_both_columns_with_their_defaults(app_conn):
    """Garde de ND-01 = B: si ces colonnes manquaient, une migration serait necessaire et la
    condition de la decision ratifiee tomberait."""
    rows = app_conn.execute(
        "SELECT column_name, column_default, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = 'stores' "
        "AND column_name IN ('timezone', 'timezone_source') ORDER BY column_name"
    ).fetchall()
    found = {name: (default, nullable) for name, default, nullable in rows}
    assert set(found) == {"timezone", "timezone_source"}, (
        "colonnes absentes: ND-01 = B exigerait alors une migration -> STOP"
    )
    for name, (default, nullable) in found.items():
        assert nullable == "NO", f"{name} doit etre NOT NULL"
        assert default is not None, f"{name} doit avoir un defaut en base"


# -- create ----------------------------------------------------------------
def test_a_store_created_without_a_timezone_is_utc_by_default(tenant_a):
    store = create_store(tenant_a.session, name="Sans fuseau")
    assert store.timezone == "UTC"
    assert store.timezone_source == "default", (
        "un UTC par defaut n'est PAS une decision du marchand: la source doit le dire"
    )


def test_a_store_created_with_a_timezone_records_it_as_explicit(tenant_a):
    store = create_store(tenant_a.session, name="Paris", timezone="Europe/Paris")
    assert store.timezone == "Europe/Paris"
    assert store.timezone_source == "explicit"


def test_an_unresolvable_timezone_is_refused_before_any_write(tenant_a):
    """Le `CHECK` de la base accepterait `aaa/bbb`: la validation semantique est applicative."""
    with pytest.raises(ConfigurationError):
        create_store(tenant_a.session, name="Fuseau bidon", timezone="aaa/bbb")
    from mervio.persistence.stores import find_stores_by_name
    assert find_stores_by_name(tenant_a.session, "Fuseau bidon") == [], (
        "refus AVANT ecriture: aucune boutique ne doit avoir ete creee"
    )


def test_the_database_check_alone_would_not_have_caught_it(app_conn):
    """Mesure, pas supposition: la contrainte de 0011 accepte un faux fuseau bien forme.

    C'est la raison d'etre de la validation applicative.
    """
    accepted = app_conn.execute(
        "SELECT 'aaa/bbb' ~ '^[A-Za-z0-9_+./-]+$'"
    ).fetchone()[0]
    assert accepted is True, "le CHECK est syntaxique, donc insuffisant"


# -- retrieve --------------------------------------------------------------
def test_the_timezone_survives_a_round_trip_through_the_database(tenant_a):
    created = create_store(tenant_a.session, name="Aller-retour", timezone="America/New_York")
    fetched = get_store(tenant_a.session, created.id)
    assert (fetched.timezone, fetched.timezone_source) == ("America/New_York", "explicit")
    assert fetched == created


def test_store_columns_and_the_dataclass_stay_aligned(tenant_a):
    """`Store(*row)` est construit positionnellement depuis `_STORE_COLUMNS`: une divergence
    d'ordre produirait des champs silencieusement permutes."""
    from dataclasses import fields
    from mervio.persistence.stores import _STORE_COLUMNS
    columns = [name.strip() for name in _STORE_COLUMNS.split(",")]
    assert [f.name for f in fields(create_store(tenant_a.session, name="Alignement"))] == columns


# -- frontiere: changer le fuseau APRES creation sort du perimetre 004.5 ----
def test_updating_the_timezone_is_refused_by_the_column_level_grant(app_conn):
    """Constat MESURE, pas suppose, et raison d'etre d'une absence.

    `0001_tenancy.py:144` accorde `UPDATE (name, currency) ON stores`: un grant au niveau
    COLONNE. `timezone` n'y est pas. Un chemin de modification exigerait donc un
    `GRANT UPDATE (timezone, timezone_source)`, c'est-a-dire une MIGRATION -- que ND-01 = B
    exclut, et que la verification demandee (<< create store, retrieve store >>) ne reclame pas.

    Ce test fige la frontiere: si un futur commit ajoutait ce grant, il echouerait ici et le
    changement serait donc explicite, jamais silencieux.
    """
    granted = app_conn.execute(
        "SELECT column_name FROM information_schema.column_privileges "
        "WHERE table_schema = 'public' AND table_name = 'stores' AND privilege_type = 'UPDATE' "
        "ORDER BY column_name"
    ).fetchall()
    columns = {row[0] for row in granted}
    assert columns == {"name", "currency"}, (
        f"privileges UPDATE sur stores: {sorted(columns)}. Si timezone y apparait, une migration "
        "a ete ajoutee: elle doit etre explicitement justifiee (ND-01 = B)."
    )


# -- la boutique alimente l'analytique -------------------------------------
def test_the_store_timezone_can_drive_the_analytical_configuration(tenant_a):
    """Jonction base -> analytique.

    L'analytique ne depend PAS de la persistance (D-004): c'est la couche applicative qui lit la
    boutique et construit la configuration. Ce test prouve que la valeur stockee est utilisable
    telle quelle comme `AnalyticsConfig.timezone`, sans traduction.
    """
    from mervio.analytics.periods import make_period
    from mervio.config import AnalyticsConfig
    from datetime import date, datetime

    store = create_store(tenant_a.session, name="Jonction", timezone="Europe/Paris")
    config = AnalyticsConfig(timezone=store.timezone)
    assert config.timezone == "Europe/Paris"

    period = make_period(date(2026, 9, 7), config.grain, config.timezone)
    assert period.start == datetime(2026, 9, 6, 22, 0), "minuit local Paris = 22 h UTC la veille"
    assert period.start_date == date(2026, 9, 7), "la date civile reste locale"


def test_a_default_store_produces_the_pre_an06_period_boundaries(tenant_a):
    """Non-regression: une boutique non configuree se comporte exactement comme avant AN-06."""
    from mervio.analytics.periods import make_period
    from mervio.config import AnalyticsConfig
    from datetime import date, datetime

    store = create_store(tenant_a.session, name="Defaut")
    period = make_period(date(2026, 9, 7), "week", AnalyticsConfig(timezone=store.timezone).timezone)
    assert period.start == datetime(2026, 9, 7, 0, 0)
