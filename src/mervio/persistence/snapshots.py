"""Instantanes de donnees: Dataset canonique <-> PostgreSQL, a l'identique.

Ecriture: une seule transaction cree l'instantane ('ingesting'), ses sources,
toutes ses lignes canoniques, puis le scelle ('completed'). Un echec annule
tout: aucun instantane partiel n'est jamais visible.

Lecture: une requete par table (jamais une par commande), ordonnee par
`position`, restitue un Dataset egal (==) a celui qui a ete persiste.

Insertion par lots `INSERT ... SELECT FROM unnest(...)`: COPY FROM est refuse par
PostgreSQL sur une table protegee par RLS.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence
from uuid import UUID

from psycopg.types.json import Jsonb
from psycopg.types.numeric import FloatLoader

from ..config import ENGINE_VERSION
from ..domain.models import Campaign, DailyAdPerformance, Dataset, Order, OrderItem, Payment, Product, Refund
from .codec import quality_from_document, quality_to_document
from .errors import ImportRejected, NotFound, SnapshotIntegrityError
from .money import money_to_db, optional_money_to_db
from .stores import _limit, _uuid, fetch_connection, fetch_store
from .tenancy import Permission, TenantSession

CSV_SOURCE = "csv"
CSV_CONNECTOR = "mervio.ingestion.csv"
#: formats de fichiers lus par les connecteurs CSV actuels
CSV_SCHEMA_VERSION = "shopify_orders_csv/1,shopify_products_csv/1,stripe_csv/1,google_ads_csv/1"
#: la normalisation est le code des connecteurs du moteur: sa version suit celle du moteur
NORMALIZATION_VERSION = f"mervio-ingestion/{ENGINE_VERSION}"

SOURCE_KINDS = ("shopify_orders", "shopify_products", "stripe", "google_ads")
_REFUND_SOURCE_KIND = {"shopify": "shopify_orders", "stripe": "stripe"}

BATCH_SIZE = 5000
_FETCH_SIZE = 10000


@dataclass(frozen=True)
class SourceFile:
    """Fichier source d'un instantane: identite par empreinte, sans nom de fichier (peut contenir une PII)."""

    kind: str
    sha256: str
    byte_size: int


@dataclass(frozen=True)
class SnapshotRecord:
    id: UUID
    organization_id: UUID
    store_id: UUID
    connection_id: UUID
    created_by: Optional[UUID]
    source: str
    connector: str
    connector_version: str
    schema_version: str
    normalization_version: str
    status: str
    ingestion_started_at: datetime
    ingested_at: Optional[datetime]
    failure_code: Optional[str]
    inputs_sha256: Optional[str]
    currency: Optional[str]
    source_period_start: Optional[datetime]
    source_period_end: Optional[datetime]
    row_count: Optional[int]
    record_counts: Optional[dict]
    synthetic: bool
    not_for_production: bool
    synthetic_manifest: Optional[dict]
    supersedes_snapshot_id: Optional[UUID]


_SNAPSHOT_COLUMNS = (
    "id, organization_id, store_id, connection_id, created_by, source, connector, connector_version, "
    "schema_version, normalization_version, status, ingestion_started_at, ingested_at, failure_code, "
    "inputs_sha256, currency, source_period_start, source_period_end, row_count, record_counts, "
    "synthetic, not_for_production, synthetic_manifest, supersedes_snapshot_id"
)


# -- ecriture ---------------------------------------------------------------------------

def write_snapshot(
    session: TenantSession,
    *,
    store_id: UUID,
    connection_id: UUID,
    dataset: Dataset,
    sources: Sequence[SourceFile],
    inputs_sha256: str,
    synthetic: bool,
    synthetic_manifest: Optional[dict] = None,
    supersedes_snapshot_id: Optional[UUID] = None,
) -> SnapshotRecord:
    """Persiste un Dataset normalise comme instantane scelle. Tout ou rien."""
    synthetic = bool(synthetic or (synthetic_manifest or {}).get("synthetic"))
    kinds = [s.kind for s in sources]
    if len(set(kinds)) != len(kinds) or any(k not in SOURCE_KINDS for k in kinds):
        raise SnapshotIntegrityError("sources d'instantane invalides ou dupliquees")

    with session.transaction(Permission.IMPORT_DATA) as conn:
        store = fetch_store(conn, session, store_id)
        connection = fetch_connection(conn, session, store.id, connection_id)
        if connection.status != "active":
            raise ImportRejected("connection_revoked", "connexion revoquee: import refuse")
        if store.currency and dataset.currency not in (store.currency, "unknown"):
            raise ImportRejected(
                "currency_mismatch_with_store",
                f"devise des donnees ({dataset.currency}) differente de la devise de la boutique ({store.currency})",
            )
        if supersedes_snapshot_id is not None:
            fetch_snapshot(conn, session, store.id, supersedes_snapshot_id)

        snapshot_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, created_by, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
            "not_for_production, synthetic_manifest, supersedes_snapshot_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ingesting', clock_timestamp(), %s, %s, %s, %s)",
            (snapshot_id, session.organization_id, store.id, connection.id, session.user_id, CSV_SOURCE,
             CSV_CONNECTOR, ENGINE_VERSION, CSV_SCHEMA_VERSION, NORMALIZATION_VERSION, synthetic, synthetic,
             Jsonb(synthetic_manifest) if synthetic_manifest is not None else None, supersedes_snapshot_id),
        )
        source_ids: Dict[str, UUID] = {}
        for source in sources:
            counts = dataset.quality.row_counts.get(source.kind)
            source_ids[source.kind] = uuid.uuid4()
            conn.execute(
                "INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, file_sha256, "
                "byte_size, rows_read, rows_accepted) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (source_ids[source.kind], session.organization_id, store.id, snapshot_id, source.kind, source.sha256,
                 source.byte_size, counts.total if counts else None, counts.accepted if counts else None),
            )

        writer = _RowWriter(conn, session.organization_id, store.id, snapshot_id, source_ids)
        counts = writer.write_dataset(dataset)
        start, end = _data_period(dataset)
        row = conn.execute(
            "UPDATE data_snapshots SET status = 'completed', ingested_at = clock_timestamp(), inputs_sha256 = %s, "
            "currency = %s, source_period_start = %s, source_period_end = %s, row_count = %s, record_counts = %s, "
            f"quality = %s WHERE organization_id = %s AND id = %s RETURNING {_SNAPSHOT_COLUMNS}",
            (inputs_sha256, dataset.currency, start, end, sum(counts.values()), Jsonb(counts),
             Jsonb(quality_to_document(dataset.quality)), session.organization_id, snapshot_id),
        ).fetchone()
    return SnapshotRecord(*row)


def record_failed_snapshot(
    session: TenantSession,
    *,
    store_id: UUID,
    connection_id: UUID,
    failure_code: str,
    synthetic: bool,
    sources: Sequence[SourceFile] = (),
) -> SnapshotRecord:
    """Trace un import refuse: instantane 'failed', sans aucune ligne canonique.

    Les empreintes des fichiers soumis sont conservees (provenance du refus),
    jamais leur contenu.
    """
    with session.transaction(Permission.IMPORT_DATA) as conn:
        store = fetch_store(conn, session, store_id)
        connection = fetch_connection(conn, session, store.id, connection_id)
        snapshot_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO data_snapshots (id, organization_id, store_id, connection_id, created_by, source, connector, "
            "connector_version, schema_version, normalization_version, status, ingestion_started_at, synthetic, "
            "not_for_production) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ingesting', clock_timestamp(), %s, %s)",
            (snapshot_id, session.organization_id, store.id, connection.id, session.user_id, CSV_SOURCE, CSV_CONNECTOR,
             ENGINE_VERSION, CSV_SCHEMA_VERSION, NORMALIZATION_VERSION, synthetic, synthetic),
        )
        for source in sources:
            conn.execute(
                "INSERT INTO snapshot_sources (id, organization_id, store_id, snapshot_id, source_kind, file_sha256, "
                "byte_size) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4(), session.organization_id, store.id, snapshot_id, source.kind, source.sha256,
                 source.byte_size),
            )
        row = conn.execute(
            "UPDATE data_snapshots SET status = 'failed', failure_code = %s WHERE organization_id = %s AND id = %s "
            f"RETURNING {_SNAPSHOT_COLUMNS}",
            (failure_code, session.organization_id, snapshot_id),
        ).fetchone()
    return SnapshotRecord(*row)


class _RowWriter:
    def __init__(self, conn, organization_id: UUID, store_id: UUID, snapshot_id: UUID, source_ids: Dict[str, UUID]):
        self.conn = conn
        self.prefix = (organization_id, store_id, snapshot_id)
        self.source_ids = source_ids

    def _source_id(self, kind: str, entity: str) -> UUID:
        try:
            return self.source_ids[kind]
        except KeyError:
            raise SnapshotIntegrityError(f"{entity}: fichier source {kind} absent de l'instantane") from None

    def _insert(self, table: str, columns: Sequence[str], casts: Sequence[str], rows: Iterable[tuple]) -> int:
        """INSERT par lots via unnest. Noms de table et colonnes: constantes du module, jamais une entree."""
        column_list = ", ".join(("organization_id", "store_id", "snapshot_id") + tuple(columns))
        arrays = ", ".join(f"%s::{cast}[]" for cast in casts)
        statement = f"INSERT INTO {table} ({column_list}) SELECT %s, %s, %s, u.* FROM unnest({arrays}) AS u"
        total = 0
        batch: List[tuple] = []
        for row in rows:
            batch.append(row)
            if len(batch) >= BATCH_SIZE:
                total += self._flush(statement, batch)
                batch = []
        if batch:
            total += self._flush(statement, batch)
        return total

    def _flush(self, statement: str, batch: List[tuple]) -> int:
        columns = [list(values) for values in zip(*batch)]
        self.conn.execute(statement, self.prefix + tuple(columns))
        return len(batch)

    def write_dataset(self, dataset: Dataset) -> Dict[str, int]:
        return {
            "products": self._products(dataset.products),
            "orders": self._orders(dataset.orders),
            "order_lines": self._order_lines(dataset.orders),
            "payments": self._payments(dataset.payments),
            "refunds": self._refunds(dataset.refunds),
            "campaigns": self._campaigns(dataset.campaigns),
            "ad_daily_performance": self._ads(dataset.ad_performance),
        }

    def _products(self, products: Dict[str, Product]) -> int:
        if not products:
            return 0
        source_id = self._source_id("shopify_products", "produits")

        def rows():
            for position, (key, p) in enumerate(products.items()):
                if key != p.sku:
                    raise SnapshotIntegrityError("produit indexe sous une cle differente de son SKU")
                yield (source_id, position, "shopify", p.sku, p.sku, p.product_id, p.title,
                       optional_money_to_db(p.unit_cogs, field="products.unit_cogs"))
        return self._insert(
            "products",
            ("snapshot_source_id", "position", "source", "source_record_id", "sku", "product_ref", "title", "unit_cogs"),
            ("uuid", "int", "text", "text", "text", "text", "text", "numeric"), rows())

    def _orders(self, orders: List[Order]) -> int:
        if not orders:
            return 0
        source_id = self._source_id("shopify_orders", "commandes")

        def rows():
            for position, o in enumerate(orders):
                yield (source_id, position, o.source, o.order_id, o.order_id, o.customer_id, o.customer_email,
                       _utc(o.created_at), o.currency,
                       money_to_db(o.subtotal, field="orders.subtotal"), money_to_db(o.discount, field="orders.discount"),
                       money_to_db(o.shipping, field="orders.shipping"), money_to_db(o.tax, field="orders.tax"),
                       money_to_db(o.total, field="orders.total"), o.financial_status)
        return self._insert(
            "orders",
            ("snapshot_source_id", "position", "source", "source_record_id", "order_ref", "customer_ref",
             "customer_email", "created_at", "currency", "subtotal", "discount", "shipping", "tax", "total",
             "financial_status"),
            ("uuid", "int", "text", "text", "text", "text", "text", "timestamptz", "text", "numeric", "numeric",
             "numeric", "numeric", "numeric", "text"), rows())

    def _order_lines(self, orders: List[Order]) -> int:
        def rows():
            for order_position, o in enumerate(orders):
                for position, item in enumerate(o.items):
                    if item.order_id != o.order_id:
                        raise SnapshotIntegrityError("ligne de commande rattachee a une autre commande")
                    yield (order_position, position, item.sku, item.title, item.quantity,
                           money_to_db(item.unit_price, field="order_lines.unit_price"), item.product_id)
        return self._insert(
            "order_lines",
            ("order_position", "position", "sku", "title", "quantity", "unit_price", "product_ref"),
            ("int", "int", "text", "text", "bigint", "numeric", "text"), rows())

    def _payments(self, payments: List[Payment]) -> int:
        if not payments:
            return 0
        source_id = self._source_id("stripe", "paiements")

        def rows():
            for position, p in enumerate(payments):
                yield (source_id, position, p.source, p.payment_id, p.payment_id, _utc(p.created_at),
                       money_to_db(p.amount, field="payments.amount"),
                       optional_money_to_db(p.fee, field="payments.fee"),
                       optional_money_to_db(p.net, field="payments.net"),
                       p.status, p.order_id, p.customer_email)
        return self._insert(
            "payments",
            ("snapshot_source_id", "position", "source", "source_record_id", "payment_ref", "created_at", "amount",
             "fee", "net", "status", "order_ref", "customer_email"),
            ("uuid", "int", "text", "text", "text", "timestamptz", "numeric", "numeric", "numeric", "text", "text",
             "text"), rows())

    def _refunds(self, refunds: List[Refund]) -> int:
        def rows():
            for position, r in enumerate(refunds):
                kind = _REFUND_SOURCE_KIND.get(r.source)
                if kind is None:
                    raise SnapshotIntegrityError("remboursement d'une source sans fichier connu")
                yield (self._source_id(kind, "remboursements"), position, r.source, r.refund_id, r.refund_id,
                       _utc(r.created_at), money_to_db(r.amount, field="refunds.amount"), r.order_id)
        return self._insert(
            "refunds",
            ("snapshot_source_id", "position", "source", "source_record_id", "refund_ref", "created_at", "amount",
             "order_ref"),
            ("uuid", "int", "text", "text", "text", "timestamptz", "numeric", "text"), rows())

    def _campaigns(self, campaigns: Dict[str, Campaign]) -> int:
        if not campaigns:
            return 0
        source_id = self._source_id("google_ads", "campagnes")

        def rows():
            for position, (key, c) in enumerate(campaigns.items()):
                if key != c.campaign_id:
                    raise SnapshotIntegrityError("campagne indexee sous une cle differente de son identifiant")
                yield (source_id, position, "google_ads", c.campaign_id, c.campaign_id, c.name, c.channel)
        return self._insert(
            "campaigns",
            ("snapshot_source_id", "position", "source", "source_record_id", "campaign_ref", "name", "channel"),
            ("uuid", "int", "text", "text", "text", "text", "text"), rows())

    def _ads(self, performance: List[DailyAdPerformance]) -> int:
        if not performance:
            return 0
        source_id = self._source_id("google_ads", "performances publicitaires")

        def rows():
            for position, a in enumerate(performance):
                if type(a.conversions) is not float:
                    raise SnapshotIntegrityError("conversions: float attendu")
                yield (source_id, position, "google_ads", f"{a.campaign_id}|{a.day.isoformat()}", a.campaign_id,
                       a.day, money_to_db(a.spend, field="ad_daily_performance.spend"), a.impressions, a.clicks,
                       a.conversions, money_to_db(a.conversion_value, field="ad_daily_performance.conversion_value"))
        return self._insert(
            "ad_daily_performance",
            ("snapshot_source_id", "position", "source", "source_record_id", "campaign_ref", "day", "spend",
             "impressions", "clicks", "conversions", "conversion_value"),
            ("uuid", "int", "text", "text", "text", "date", "numeric", "bigint", "bigint", "float8", "numeric"),
            rows())


def _utc(moment: datetime) -> datetime:
    """Le domaine manipule des datetimes naifs UTC (ingestion.base.parse_datetime)."""
    if moment.tzinfo is not None:
        raise SnapshotIntegrityError("datetime avec fuseau: le domaine attend du UTC naif")
    return moment.replace(tzinfo=timezone.utc)


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _data_period(dataset: Dataset):
    moments: List[datetime] = [o.created_at for o in dataset.orders]
    moments += [p.created_at for p in dataset.payments]
    moments += [r.created_at for r in dataset.refunds]
    moments += [datetime.combine(a.day, datetime.min.time()) for a in dataset.ad_performance]
    if not moments:
        return None, None
    return _utc(min(moments)), _utc(max(moments))


# -- lecture ------------------------------------------------------------------------------

def fetch_snapshot(conn, session: TenantSession, store_id: UUID, snapshot_id: UUID) -> SnapshotRecord:
    row = conn.execute(
        f"SELECT {_SNAPSHOT_COLUMNS} FROM data_snapshots WHERE organization_id = %s AND store_id = %s AND id = %s",
        (session.organization_id, _uuid(store_id, "store"), _uuid(snapshot_id, "snapshot")),
    ).fetchone()
    if row is None:
        raise NotFound("snapshot")
    return SnapshotRecord(*row)


def get_snapshot(session: TenantSession, store_id: UUID, snapshot_id: UUID) -> SnapshotRecord:
    with session.transaction(Permission.READ) as conn:
        return fetch_snapshot(conn, session, store_id, snapshot_id)


def list_snapshots(session: TenantSession, store_id: UUID, *, limit: int = 50) -> List[SnapshotRecord]:
    with session.transaction(Permission.READ) as conn:
        fetch_store(conn, session, store_id)
        rows = conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM data_snapshots WHERE organization_id = %s AND store_id = %s "
            "ORDER BY ingestion_started_at DESC, id LIMIT %s",
            (session.organization_id, store_id, _limit(limit)),
        ).fetchall()
    return [SnapshotRecord(*r) for r in rows]


def find_completed_snapshot(session: TenantSession, store_id: UUID, inputs_sha256: str) -> Optional[SnapshotRecord]:
    with session.transaction(Permission.READ) as conn:
        fetch_store(conn, session, store_id)
        row = conn.execute(
            f"SELECT {_SNAPSHOT_COLUMNS} FROM data_snapshots WHERE organization_id = %s AND store_id = %s "
            "AND inputs_sha256 = %s AND status = 'completed'",
            (session.organization_id, store_id, inputs_sha256),
        ).fetchone()
    return SnapshotRecord(*row) if row else None


def snapshot_sources(session: TenantSession, store_id: UUID, snapshot_id: UUID) -> List[dict]:
    with session.transaction(Permission.READ_PROVENANCE) as conn:
        fetch_snapshot(conn, session, store_id, snapshot_id)
        cur = conn.execute(
            "SELECT id, source_kind, file_sha256, byte_size, rows_read, rows_accepted FROM snapshot_sources "
            "WHERE organization_id = %s AND store_id = %s AND snapshot_id = %s ORDER BY source_kind",
            (session.organization_id, store_id, snapshot_id),
        )
        names = [c.name for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def load_dataset(session: TenantSession, store_id: UUID, snapshot_id: UUID,
                 *, permission: Permission = Permission.READ) -> Dataset:
    """Reconstruit le Dataset exact d'un instantane scelle."""
    with session.transaction(permission) as conn:
        record = fetch_snapshot(conn, session, store_id, snapshot_id)
        if record.status != "completed":
            raise SnapshotIntegrityError(f"instantane {record.status}: seul un instantane complet est analysable")
        reader = _RowReader(conn, session.organization_id, record.store_id, record.id)
        dataset = Dataset(
            orders=reader.orders(),
            products=reader.products(),
            payments=reader.payments(),
            refunds=reader.refunds(),
            campaigns=reader.campaigns(),
            ad_performance=reader.ads(),
            quality=quality_from_document(_quality(conn, session, record.id)),
            currency=record.currency,
        )
        observed = {
            "products": len(dataset.products), "orders": len(dataset.orders),
            "order_lines": sum(len(o.items) for o in dataset.orders), "payments": len(dataset.payments),
            "refunds": len(dataset.refunds), "campaigns": len(dataset.campaigns),
            "ad_daily_performance": len(dataset.ad_performance),
        }
        if observed != record.record_counts:
            raise SnapshotIntegrityError("contenu relu different des compteurs scelles de l'instantane")
    dataset.compute_customer_index()
    return dataset


def _quality(conn, session: TenantSession, snapshot_id: UUID) -> dict:
    return conn.execute(
        "SELECT quality FROM data_snapshots WHERE organization_id = %s AND id = %s",
        (session.organization_id, snapshot_id),
    ).fetchone()[0]


class _RowReader:
    def __init__(self, conn, organization_id: UUID, store_id: UUID, snapshot_id: UUID):
        self.conn = conn
        self.params = (organization_id, store_id, snapshot_id)

    def _rows(self, columns: str, table: str, order: str):
        cursor = self.conn.cursor(name=f"load_{table}")
        cursor.itersize = _FETCH_SIZE
        # numeric -> float directement: float(texte) == float(Decimal(texte)), arrondi correct
        cursor.adapters.register_loader("numeric", FloatLoader)
        cursor.execute(
            f"SELECT {columns} FROM {table} WHERE organization_id = %s AND store_id = %s AND snapshot_id = %s "
            f"ORDER BY {order}",
            self.params,
        )
        try:
            yield from cursor
        finally:
            cursor.close()

    def products(self) -> Dict[str, Product]:
        return {sku: Product(product_ref, sku, title, unit_cogs)
                for sku, product_ref, title, unit_cogs in
                self._rows("sku, product_ref, title, unit_cogs", "products", "position")}

    def orders(self) -> List[Order]:
        orders = [
            Order(order_id=order_ref, customer_id=customer_ref, created_at=_naive(created_at), currency=currency,
                  subtotal=subtotal, discount=discount, shipping=shipping, tax=tax, total=total,
                  financial_status=financial_status, customer_email=customer_email, source=source)
            for (source, order_ref, customer_ref, customer_email, created_at, currency, subtotal, discount, shipping,
                 tax, total, financial_status) in self._rows(
                "source, order_ref, customer_ref, customer_email, created_at, currency, subtotal, discount, shipping, "
                "tax, total, financial_status", "orders", "position")
        ]
        for order_position, position, sku, title, quantity, unit_price, product_ref in self._rows(
                "order_position, position, sku, title, quantity, unit_price, product_ref", "order_lines",
                "order_position, position"):
            order = orders[order_position]
            if position != len(order.items):
                raise SnapshotIntegrityError("rang de ligne de commande discontinu")
            order.items.append(OrderItem(order.order_id, sku, title, quantity, unit_price, product_ref))
        return orders

    def payments(self) -> List[Payment]:
        return [Payment(payment_ref, _naive(created_at), amount, fee, net, status, order_ref, customer_email, source)
                for source, payment_ref, created_at, amount, fee, net, status, order_ref, customer_email in self._rows(
                    "source, payment_ref, created_at, amount, fee, net, status, order_ref, customer_email",
                    "payments", "position")]

    def refunds(self) -> List[Refund]:
        return [Refund(refund_ref, _naive(created_at), amount, order_ref, source)
                for source, refund_ref, created_at, amount, order_ref in self._rows(
                    "source, refund_ref, created_at, amount, order_ref", "refunds", "position")]

    def campaigns(self) -> Dict[str, Campaign]:
        return {ref: Campaign(ref, name, channel)
                for ref, name, channel in self._rows("campaign_ref, name, channel", "campaigns", "position")}

    def ads(self) -> List[DailyAdPerformance]:
        return [DailyAdPerformance(day, ref, spend, impressions, clicks, conversions, conversion_value)
                for ref, day, spend, impressions, clicks, conversions, conversion_value in self._rows(
                    "campaign_ref, day, spend, impressions, clicks, conversions, conversion_value",
                    "ad_daily_performance", "position")]
