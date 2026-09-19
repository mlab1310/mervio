"""Aides partagees des tests PostgreSQL (importables, contrairement a conftest)."""
from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import UUID

from mervio.identity import MasterKey

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "data" / "sample"
SAMPLE_FILES = {"shopify_orders": str(SAMPLE_DIR / "shopify_orders.csv"),
                "shopify_products": str(SAMPLE_DIR / "shopify_products.csv"),
                "stripe": str(SAMPLE_DIR / "stripe_transactions.csv"),
                "google_ads": str(SAMPLE_DIR / "google_ads.csv")}

#: meme taille que les tests du generateur (tests/test_synthetic_generator.py)
SYNTHETIC_CONFIG = dict(orders=1500, days=56, seed=7)

#: cle maitre d'identite FACTICE de la session de test (004.4.2): tiree a l'execution, jamais
#: ecrite dans le depot. Les cles d'organisation qui en derivent sont stables pendant la session.
TEST_MASTER_KEY_HEX = secrets.token_hex(32)
TEST_MASTER_KEY = MasterKey.from_hex(TEST_MASTER_KEY_HEX)


def engine_files(directory: Path) -> dict:
    return {"shopify_orders": str(directory / "shopify_orders.csv"),
            "shopify_products": str(directory / "shopify_products.csv"),
            "stripe": str(directory / "stripe_transactions.csv"),
            "google_ads": str(directory / "google_ads.csv")}


def org_identity(tenant):
    """Cle d'identite de l'organisation du tenant (creee si absente): la meme que l'import persiste.

    Les comparaisons CLI == persiste passent cette cle EXPLICITE au chemin CLI (D-058): sans elle,
    la CLI tire une cle ephemere et les references client different par construction.
    """
    from mervio.persistence.identity_keys import ensure_identity_key
    return ensure_identity_key(tenant.session, TEST_MASTER_KEY)


def analysis_day(manifest: dict) -> date:
    from mervio.synthetic.evaluation import analysis_day as day
    return day(manifest)


@dataclass
class Tenant:
    organization_id: UUID
    owner_id: UUID
    store_id: UUID
    connection_id: UUID
    session: object


def make_tenant(database, name: str, *, currency: str | None = None) -> Tenant:
    """Organisation + proprietaire + boutique + connexion CSV."""
    from mervio.persistence.stores import create_csv_connection, create_store
    from mervio.persistence.tenancy import TenantContext, TenantSession, create_organization, ensure_user
    owner_id = ensure_user(database, f"test|{name}|owner")
    organization_id = create_organization(database, owner_user_id=owner_id, name=f"Organisation {name}")
    session = TenantSession(database, TenantContext(organization_id, owner_id))
    store = create_store(session, name=f"Boutique {name}", currency=currency)
    connection = create_csv_connection(session, store_id=store.id, label=f"Imports CSV {name}")
    return Tenant(organization_id, owner_id, store.id, connection.id, session)
