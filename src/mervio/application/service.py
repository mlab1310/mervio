"""Service applicatif: le point d'entree unique au-dessus du moteur.

C'est exactement ce qu'un handler FastAPI appellera demain:

    POST /analyses          -> analyze_dataset(AnalysisRequest(...))
    GET  /analyses/{id}     -> AnalysisResult.summary()
    GET  /analyses/{id}/report -> AnalysisResult.report

Ce module N'EFFECTUE AUCUN CALCUL metier: il orchestre ingestion, analyse et
restitution. Le moteur analytique reste la source de verite.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from ..analytics.pipeline import SourcePaths, run_analysis
from ..config import AnalyticsConfig
from ..errors import InsufficientDataError, MervioError
from ..identity import CustomerIdentity
from ..logging_config import get_logger
from .imports import REQUIRED_SOURCES, FileValidation, validate_many

log = get_logger("application.service")


@dataclass
class AnalysisRequest:
    shopify_orders: Optional[str] = None
    shopify_products: Optional[str] = None
    stripe: Optional[str] = None
    google_ads: Optional[str] = None
    config: AnalyticsConfig = field(default_factory=AnalyticsConfig)
    today: Optional[date] = None
    #: marque le jeu de donnees comme synthetique (commande demo)
    synthetic: bool = False
    label: str = ""
    #: cle des references client; None = cle ephemere tiree pour cette analyse (D-058)
    identity: Optional[CustomerIdentity] = field(default=None, repr=False)

    def provided(self) -> Dict[str, Optional[str]]:
        return {
            "shopify_orders": self.shopify_orders,
            "shopify_products": self.shopify_products,
            "stripe": self.stripe,
            "google_ads": self.google_ads,
        }

    def to_source_paths(self) -> SourcePaths:
        return SourcePaths(
            shopify_orders=self.shopify_orders, shopify_products=self.shopify_products,
            stripe=self.stripe, google_ads=self.google_ads,
        )


@dataclass
class AnalysisResult:
    analysis_id: str
    status: str  # completed | rejected
    generated_at: str
    validations: List[FileValidation]
    report: Optional[dict] = None
    error: Optional[str] = None
    synthetic: bool = False
    label: str = ""
    written_files: List[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == "completed" and self.report is not None

    def summary(self) -> dict:
        """Charge utile legere pour un futur GET /analyses/{id}."""
        health = (self.report or {}).get("business_health_score", {})
        period = (self.report or {}).get("period", {}).get("current", {})
        return {
            "analysis_id": self.analysis_id, "status": self.status,
            "generated_at": self.generated_at, "synthetic": self.synthetic,
            "label": self.label, "error": self.error,
            "business_health_score": health.get("score"),
            "period": period,
            "sources": [v.to_dict() for v in self.validations],
        }

    def data_quality_document(self) -> dict:
        report = self.report or {}
        return {
            "analysis_id": self.analysis_id,
            "generated_at": self.generated_at,
            "synthetic_data": self.synthetic,
            "label": self.label,
            "period": report.get("period"),
            "validations": [v.to_dict() for v in self.validations],
            "data_quality": report.get("data_quality"),
            "limitations": report.get("limitations", []),
        }


def _new_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"


def annotate_report(report: dict, *, synthetic: bool, label: str) -> dict:
    """Metadonnees de livraison ajoutees au rapport du moteur (CLI et analyses persistees)."""
    if synthetic:
        report["_meta"]["dataset_is_synthetic"] = True
    report["_meta"]["analysis_label"] = label
    return report


def analyze_dataset(request: AnalysisRequest) -> AnalysisResult:
    """Valide les fichiers puis lance l'analyse. Ne leve pas: rapporte."""
    analysis_id = _new_id()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    validations = validate_many({k: v for k, v in request.provided().items() if v})

    def rejected(message: str) -> AnalysisResult:
        log.warning("analyse %s rejetee: %s", analysis_id, message)
        return AnalysisResult(analysis_id, "rejected", now, validations,
                              error=message, synthetic=request.synthetic, label=request.label)

    if not validations:
        return rejected("aucun fichier fourni")

    usable = {v.source for v in validations if v.usable}
    missing_required = [s for s in REQUIRED_SOURCES if s not in usable]
    if missing_required:
        invalid = [f"{v.source}: {v.error}" for v in validations if not v.usable and v.error]
        detail = " | ".join(invalid) if invalid else "source absente"
        return rejected(
            "source obligatoire indisponible (" + ", ".join(missing_required) + "): " + detail
        )

    try:
        report = run_analysis(request.to_source_paths(), request.config, today=request.today,
                              identity=request.identity)
    except InsufficientDataError as exc:
        return rejected(f"donnees insuffisantes: {exc}")
    except MervioError as exc:
        return rejected(f"echec de l'analyse: {exc}")

    annotate_report(report, synthetic=request.synthetic, label=request.label)

    log.info("analyse %s terminee (score=%s)", analysis_id,
             report["business_health_score"]["score"])
    return AnalysisResult(analysis_id, "completed", now, validations, report=report,
                          synthetic=request.synthetic, label=request.label)
