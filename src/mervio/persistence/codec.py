"""Serialisations stables: qualite de donnee, rapport, configuration, empreintes.

Aucune de ces fonctions ne calcule un KPI. Elles transportent les objets du
domaine et du moteur sans en changer une valeur.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Dict, Mapping

from ..config import AnalyticsConfig
from ..domain.quality import DataIssue, DataQualityReport, FieldQuality, QualityStatus, RowCount
from .errors import SnapshotIntegrityError

QUALITY_DOCUMENT_VERSION = 1


# -- rapport ----------------------------------------------------------------------------

def serialize_report(report: dict) -> str:
    """Octets du livrable `report.json` de la CLI (reporting.writers), a l'identique.

    Toute divergence avec `write_outputs` est detectee par
    tests/test_persistence_reports.py (comparaison au fichier ecrit par la CLI).
    """
    return json.dumps(report, indent=2, ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- configuration d'analyse -------------------------------------------------------------

def config_document(config: AnalyticsConfig) -> dict:
    return asdict(config)


def config_sha256(config: AnalyticsConfig) -> str:
    canonical = json.dumps(config_document(config), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# -- empreinte d'import -------------------------------------------------------------------

def inputs_sha256(*, file_hashes: Mapping[str, str], connector: str, schema_version: str,
                  normalization_version: str, synthetic: bool) -> str:
    """Empreinte des ENTREES d'un instantane: octets des fichiers + versions de normalisation.

    Memes fichiers, memes versions de connecteur et de normalisation -> meme Dataset
    (les connecteurs sont deterministes): c'est la cle d'idempotence d'import.
    """
    document = {
        "files": dict(sorted(file_hashes.items())), "connector": connector, "schema_version": schema_version,
        "normalization_version": normalization_version, "synthetic": bool(synthetic),
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path) -> tuple:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# -- qualite de donnee (sans perte) ---------------------------------------------------------

def _exact_float(value, what: str) -> str:
    if type(value) is not float:
        raise SnapshotIntegrityError(f"{what}: float attendu, {type(value).__name__} recu")
    return repr(value)


def quality_to_document(quality: DataQualityReport) -> dict:
    """Document JSON sans perte d'un DataQualityReport.

    jsonb ne conserve pas l'ordre des cles d'objet: tout ce qui est ordonne
    (dictionnaires du rapport qualite) est stocke en listes. Les flottants sont
    stockes par leur repr, restituee a l'identique.
    """
    return {
        "version": QUALITY_DOCUMENT_VERSION,
        "fields": [[key, f.field_name, f.status.value, _exact_float(f.coverage, "coverage"), f.note]
                   for key, f in quality.fields.items()],
        "issues": [[i.source, i.kind, i.severity, i.message, i.count] for i in quality.issues],
        "sources_loaded": list(quality.sources_loaded),
        "row_counts": [[key, r.source, r.total, r.accepted] for key, r in quality.row_counts.items()],
        "observed_currencies": [[source, list(codes)] for source, codes in quality.observed_currencies.items()],
    }


def quality_from_document(document: dict) -> DataQualityReport:
    if document.get("version") != QUALITY_DOCUMENT_VERSION:
        raise SnapshotIntegrityError(f"version de document qualite inconnue: {document.get('version')!r}")
    fields: Dict[str, FieldQuality] = {
        key: FieldQuality(name, QualityStatus(status), float(coverage), note)
        for key, name, status, coverage, note in document["fields"]
    }
    return DataQualityReport(
        fields=fields,
        issues=[DataIssue(source, kind, severity, message, count)
                for source, kind, severity, message, count in document["issues"]],
        sources_loaded=list(document["sources_loaded"]),
        row_counts={key: RowCount(source, total, accepted) for key, source, total, accepted in document["row_counts"]},
        observed_currencies={source: list(codes) for source, codes in document["observed_currencies"]},
    )
