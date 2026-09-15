"""Garantie: un schema externe non-Shopify est REFUSE, jamais devine.

Contexte (mission 001.6): un dataset Kaggle "Shopify Sales Dataset for ML &
EDA" expose 17 colonnes qui ne correspondent a aucune colonne d'un export
Shopify natif. Le risque n'est pas que Mervio echoue: c'est qu'il "reussisse"
en devinant un mapping et produise des chiffres faux.

Ces tests figent le comportement attendu: detection impossible, validation
invalide, analyse rejetee. Aucun de ces tests ne depend du fichier Kaggle
lui-meme; ils sont construits a partir de l'ENTETE annoncee.
"""
from __future__ import annotations

import pytest

from mervio.application.imports import UnknownSourceError, detect_source, validate_file
from mervio.application.inspector import inspect_file
from mervio.application.service import AnalysisRequest, analyze_dataset

#: entete exacte annoncee pour le dataset externe
KAGGLE_COLUMNS = (
    "order_id", "order_date", "customer_id", "product_id", "product_category",
    "product_price", "discount_percent", "quantity", "customer_country",
    "traffic_source", "payment_method", "shipping_cost", "rating", "is_returned",
    "discounted_price", "revenue", "profit",
)

#: lignes fabriquees pour le test, sans aucun lien avec le dataset reel
_ROWS = (
    "ORD-1,2026-03-01,CUST-1,P-1,accessoires,49.00,10,2,FR,organic,card,4.90,4,False,44.10,88.20,83.30\n"
    "ORD-2,2026-03-02,CUST-2,P-2,maroquinerie,189.00,0,1,BE,paid_search,paypal,0.00,5,True,189.00,189.00,189.00\n"
)


@pytest.fixture
def kaggle_like(tmp_path):
    path = tmp_path / "external.csv"
    path.write_text(",".join(KAGGLE_COLUMNS) + "\n" + _ROWS, encoding="utf-8")
    return str(path)


def test_schema_has_zero_column_in_common_with_shopify():
    """Aucun recouvrement: c'est la raison de fond du rejet."""
    from mervio.ingestion.shopify import REQUIRED_ORDER_COLUMNS
    assert set(KAGGLE_COLUMNS).isdisjoint(set(REQUIRED_ORDER_COLUMNS))


def test_source_detection_refuses_instead_of_guessing(kaggle_like):
    with pytest.raises(UnknownSourceError) as exc:
        detect_source(kaggle_like)
    assert "signature" in str(exc.value)


def test_validation_marks_the_file_invalid(kaggle_like):
    validation = validate_file(kaggle_like)
    assert validation.status == "invalid"
    assert validation.usable is False
    assert validation.source == "unknown"


def test_forcing_the_shopify_connector_still_fails_cleanly(kaggle_like):
    """Meme en imposant la source, les colonnes obligatoires manquent."""
    validation = validate_file(kaggle_like, "shopify_orders")
    assert validation.status == "invalid"
    assert "manquantes" in validation.error


def test_analysis_is_rejected_and_produces_nothing(kaggle_like):
    result = analyze_dataset(AnalysisRequest(shopify_orders=kaggle_like))
    assert result.status == "rejected"
    assert result.report is None


def test_a_profit_column_is_never_adopted_as_contribution_profit(kaggle_like):
    """Point critique: une colonne nommee 'profit' ne doit jamais alimenter
    la profitabilite Mervio. Sa definition externe (ici revenue - shipping_cost)
    exclut COGS, frais de paiement, publicite et remboursements."""
    result = analyze_dataset(AnalysisRequest(shopify_orders=kaggle_like))
    assert result.report is None  # rien n'est produit, donc rien n'est contamine
    from mervio.analytics.profitability import REQUIRED_COMPONENTS
    assert "cogs" in REQUIRED_COMPONENTS and "payment_fees" in REQUIRED_COMPONENTS


def test_inspector_reads_the_file_even_though_the_engine_refuses_it(kaggle_like):
    """L'inspecteur doit rester utilisable sur un format inconnu: c'est son role."""
    inspection = inspect_file(kaggle_like)
    assert inspection.readable is True
    assert inspection.column_count == 17
    assert {c.name for c in inspection.columns} == set(KAGGLE_COLUMNS)


def test_inspector_flags_the_customer_identifier_as_personal(kaggle_like):
    assert "customer_id" in inspect_file(kaggle_like).personal_data_columns


def test_cli_explains_that_the_file_was_unrecognised_not_missing(kaggle_like, capsys):
    """Dire 'aucun fichier fourni' quand un fichier a bien ete passe envoie
    l'utilisateur chercher le probleme au mauvais endroit."""
    from mervio.cli import main
    assert main(["validate", "--file", kaggle_like]) == 2
    err = capsys.readouterr().err
    assert "n'a pu etre identifie" in err
    assert "inspect --file" in err
    assert "aucun fichier fourni" not in err
