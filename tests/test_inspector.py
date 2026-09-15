"""Tests de l'inspecteur de CSV inconnu (mission 001.6)."""
from __future__ import annotations

import json

import pytest

from mervio.application.inspector import inspect_file

HEADER = "order_id,day,amount,currency,customer_email,label\n"
ROW = "A100,2026-06-01,120.50,EUR,a@x.com,Produit A\n"


def write(tmp_path, name, content, encoding="utf-8"):
    path = tmp_path / name
    path.write_text(content, encoding=encoding)
    return str(path)


def test_missing_file_is_reported_not_raised():
    inspection = inspect_file("/does/not/exist.csv")
    assert inspection.readable is False and inspection.error


def test_basic_structure(tmp_path):
    data = inspect_file(write(tmp_path, "f.csv", HEADER + ROW * 3)).to_dict()
    assert data["rows"] == 3 and data["columns_count"] == 6
    assert data["delimiter"] == ","
    # utf-8-sig est tente en premier et lit aussi l'UTF-8 sans BOM
    assert data["encoding"] == "utf-8-sig"


def test_types_are_inferred(tmp_path):
    columns = {c["column"]: c["inferred_type"]
               for c in inspect_file(write(tmp_path, "f.csv", HEADER + ROW * 3)).to_dict()["columns"]}
    assert columns["day"] == "date"
    assert columns["amount"] == "decimal"
    assert columns["currency"] == "currency_code"
    assert columns["label"] == "text"


def test_semicolon_and_cp1252_are_handled(tmp_path):
    content = (HEADER + ROW).replace(",", ";").replace("Produit A", "Caf\u00e9")
    data = inspect_file(write(tmp_path, "f.csv", content, encoding="cp1252")).to_dict()
    assert data["delimiter"] == ";" and data["encoding"] == "cp1252"
    assert data["columns_count"] == 6


def test_missing_values_are_counted(tmp_path):
    content = HEADER + ROW + "A101,2026-06-02,,EUR,,Produit B\n"
    columns = {c["column"]: c for c in inspect_file(write(tmp_path, "f.csv", content)).to_dict()["columns"]}
    assert columns["amount"]["missing"] == 1
    assert columns["customer_email"]["missing_pct"] == pytest.approx(0.5)


def test_duplicate_rows_are_counted(tmp_path):
    data = inspect_file(write(tmp_path, "f.csv", HEADER + ROW + ROW + ROW)).to_dict()
    assert data["duplicate_rows"] == 2


def test_period_is_computed_on_the_whole_file_not_a_sample(tmp_path):
    """Une periode calculee sur un echantillon tronque annoncerait une fin fausse."""
    from datetime import date, timedelta
    rows = "".join(
        f"A{i},{date(2026, 1, 1) + timedelta(days=i)},10.00,EUR,a@x.com,P\n"
        for i in range(2500)
    )
    data = inspect_file(write(tmp_path, "f.csv", HEADER + rows)).to_dict()
    assert data["period"]["start"] == "2026-01-01"
    assert data["period"]["dated_rows"] == 2500
    assert data["period"]["end"] > "2026-08-01"


def test_currencies_are_listed(tmp_path):
    content = HEADER + ROW + "A101,2026-06-02,90.00,USD,b@x.com,P\n"
    data = inspect_file(write(tmp_path, "f.csv", content)).to_dict()
    assert data["currencies"] == ["EUR", "USD"]


def test_personal_columns_are_flagged(tmp_path):
    data = inspect_file(write(tmp_path, "f.csv", HEADER + ROW * 3)).to_dict()
    assert "customer_email" in data["personal_data_columns"]


def test_numeric_and_date_columns_are_not_false_positives(tmp_path):
    """'Shipping' (montant) et 'Day' (date) ne sont pas des donnees personnelles."""
    header = "Day,Shipping,Campaign ID,Lineitem name\n"
    rows = "2026-06-01,5.90,11122334,Bracelet Acier\n" * 5
    data = inspect_file(write(tmp_path, "f.csv", header + rows)).to_dict()
    assert data["personal_data_columns"] == []


def test_inspection_never_returns_a_cell_value(tmp_path):
    content = HEADER + "A100,2026-06-01,120.50,EUR,secret.person@client.fr,Produit A\n"
    payload = json.dumps(inspect_file(write(tmp_path, "f.csv", content)).to_dict())
    assert "secret.person@client.fr" not in payload
    assert "120.50" not in payload


def test_sensitive_findings_are_surfaced(tmp_path):
    header = "order_id,Card Number\n"
    data = inspect_file(write(tmp_path, "f.csv", header + "A1,4242424242424242\n")).to_dict()
    assert any(f["kind"] == "card_number" for f in data["sensitive_findings"])
    assert "4242424242424242" not in json.dumps(data)


def test_granularity_is_always_labelled_as_a_guess(tmp_path):
    data = inspect_file(write(tmp_path, "f.csv", HEADER + ROW * 3)).to_dict()
    assert "guess" in data["granularity"] and "evidence" in data["granularity"]


def test_line_item_granularity_is_detected(tmp_path):
    content = HEADER + "A100,2026-06-01,10,EUR,a@x.com,P1\n" * 1 + \
        "A100,2026-06-01,20,EUR,a@x.com,P2\n" + "A101,2026-06-02,30,EUR,b@x.com,P3\n"
    data = inspect_file(write(tmp_path, "f.csv", content)).to_dict()
    assert "article" in data["granularity"]["guess"]


def test_header_only_file_is_reported(tmp_path):
    inspection = inspect_file(write(tmp_path, "f.csv", HEADER))
    assert inspection.row_count == 0 and inspection.error


def test_cli_inspect_writes_json(tmp_path, sample_paths):
    from mervio.cli import main
    out = tmp_path / "insp"
    assert main(["inspect", "--file", sample_paths.shopify_orders, "--out", str(out)]) == 0
    payload = json.loads((out / "inspection.json").read_text(encoding="utf-8"))
    assert payload[0]["rows"] > 0


def test_cli_inspect_returns_error_code_on_unreadable_file(tmp_path):
    from mervio.cli import main
    assert main(["inspect", "--file", str(tmp_path / "nope.csv")]) == 1


# --- bugs decouverts en confrontant l'inspecteur a un fichier de 60 000 lignes ---
def test_distinct_counts_cover_the_whole_file_not_the_sample(tmp_path):
    """Bug 001.6: la cardinalite etait calculee sur 2 000 lignes. Sur un fichier
    de 60 000 lignes, un identifiant unique affichait '2000 distinct', ce qui
    fausse la deduction de granularite."""
    from mervio.application.inspector import PROFILE_ROWS
    rows = "".join(f"ID{i},2026-01-01,10.00\n" for i in range(PROFILE_ROWS + 1500))
    path = tmp_path / "big.csv"
    path.write_text("ident,day,amount\n" + rows, encoding="utf-8")
    data = inspect_file(str(path)).to_dict()
    columns = {c["column"]: c for c in data["columns"]}
    assert data["rows"] == PROFILE_ROWS + 1500
    assert columns["ident"]["distinct"] == PROFILE_ROWS + 1500


def test_missing_counts_cover_the_whole_file(tmp_path):
    from mervio.application.inspector import PROFILE_ROWS
    rows = "".join(f"ID{i},2026-01-01,\n" for i in range(PROFILE_ROWS + 500))
    path = tmp_path / "big.csv"
    path.write_text("ident,day,amount\n" + rows, encoding="utf-8")
    columns = {c["column"]: c for c in inspect_file(str(path)).to_dict()["columns"]}
    assert columns["amount"]["missing"] == PROFILE_ROWS + 500
    assert columns["amount"]["missing_pct"] == pytest.approx(1.0)


def test_granularity_compares_against_total_rows(tmp_path):
    """Un identifiant qui se repete sur l'ensemble du fichier doit donner
    'ligne d'article', meme si l'echantillon de tete semble unique."""
    from mervio.application.inspector import PROFILE_ROWS
    head = "".join(f"ORD{i},2026-01-01,10.00\n" for i in range(PROFILE_ROWS))
    tail = "".join(f"ORD{i % 50},2026-01-02,10.00\n" for i in range(3000))
    path = tmp_path / "big.csv"
    path.write_text("order_id,day,amount\n" + head + tail, encoding="utf-8")
    data = inspect_file(str(path)).to_dict()
    assert "article" in data["granularity"]["guess"]


def test_customer_identifier_is_flagged_whatever_its_type(tmp_path):
    """Bug 001.6: un customer_id TEXTE etait signale, un customer_id NUMERIQUE
    ne l'etait pas. Le type ne doit pas decider de la sensibilite."""
    numeric = tmp_path / "n.csv"
    numeric.write_text("customer_id,amount\n10234,5.00\n10235,6.00\n", encoding="utf-8")
    textual = tmp_path / "t.csv"
    textual.write_text("customer_id,amount\nCUST-1,5.00\nCUST-2,6.00\n", encoding="utf-8")
    assert "customer_id" in inspect_file(str(numeric)).personal_data_columns
    assert "customer_id" in inspect_file(str(textual)).personal_data_columns
