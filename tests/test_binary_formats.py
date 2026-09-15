"""Fichiers non CSV (Mission 003).

Decouvert sur un vrai fichier .xlsx: latin-1 decode n'importe quel octet,
l'inspecteur annoncait donc un fichier "lisible" de 49 817 lignes et d'une
colonne binaire, avec 507 doublons. La validation refusait, mais en citant des
colonnes manquantes au lieu du vrai motif.
"""
from __future__ import annotations

import zipfile

import pytest

from mervio.application.imports import validate_file
from mervio.application.inspector import inspect_file
from mervio.cli import main
from mervio.ingestion.base import unsupported_format

HEADER = "Name,Email,Created at,Currency,Subtotal,Lineitem quantity,Lineitem price\n"
ROW = "#1001,,2026-09-08 10:00:00 +0200,EUR,10.00,1,10.00\n"


def _xlsx(tmp_path):
    path = tmp_path / "orders_export.xlsx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/sharedStrings.xml", "<sst><si><t>" + HEADER + "</t></si></sst>")
    return path


FORMATS = {
    "xlsx": (_xlsx, "xlsx"),
    "xls": (lambda tmp: _write(tmp / "orders.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600), ".xls"),
    "pdf": (lambda tmp: _write(tmp / "orders.pdf", b"%PDF-1.7\n" + HEADER.encode()), "PDF"),
    "utf16": (lambda tmp: _write(tmp / "orders.csv", (HEADER + ROW).encode("utf-16")), "UTF-16"),
    "nul_bytes": (lambda tmp: _write(tmp / "orders.csv", HEADER.encode() + b"\x00\x01\x02" * 50), "binaire"),
}


def _write(path, data):
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_non_csv_file_is_named_as_such(tmp_path, kind):
    build, label = FORMATS[kind]
    path = build(tmp_path)
    reason = unsupported_format(path)
    assert reason and label in reason and "CSV UTF-8" in reason


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_inspection_refuses_instead_of_describing_garbage(tmp_path, kind):
    inspection = inspect_file(FORMATS[kind][0](tmp_path))
    assert inspection.readable is False
    assert inspection.row_count == 0 and inspection.columns == []
    assert "CSV UTF-8" in inspection.error


@pytest.mark.parametrize("kind", sorted(FORMATS))
def test_validation_gives_the_real_reason(tmp_path, kind):
    path = FORMATS[kind][0](tmp_path)
    detected = validate_file(str(path))
    forced = validate_file(str(path), "shopify_orders")
    for validation in (detected, forced):
        assert validation.status == "invalid"
        assert "CSV UTF-8" in validation.error
        assert "Colonnes obligatoires manquantes" not in validation.error


def test_cli_inspect_reports_the_file_as_unreadable(tmp_path, capsys):
    assert main(["inspect", "--file", str(_xlsx(tmp_path))]) == 1
    assert "ILLISIBLE" in capsys.readouterr().out


@pytest.mark.parametrize("content", [
    HEADER + ROW,
    "\ufeff" + HEADER + ROW,
    (HEADER + ROW).replace(",", ";"),
    'Name,Lineitem name,Created at,Lineitem quantity,Lineitem price\n#1001,"Sac, cuir ""noir""",2026-09-08,1,"1,234.50"\n',
])
def test_text_csv_variants_are_still_accepted(tmp_path, content):
    path = tmp_path / "orders.csv"
    path.write_text(content, encoding="utf-8")
    assert unsupported_format(path) is None
    assert inspect_file(path).readable is True


def test_cp1252_accents_are_not_mistaken_for_binary(tmp_path):
    path = tmp_path / "orders.csv"
    path.write_bytes((HEADER + ROW.replace("#1001", "#1001 cafe")).encode() + "Réf ééé\n".encode("cp1252"))
    assert unsupported_format(path) is None
