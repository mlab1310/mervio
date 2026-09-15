"""Couche applicative: orchestration entre le domaine et les interfaces.

Aucune logique de calcul ici. Cette couche est ce qu'une API exposera.
"""
from .imports import FileValidation, detect_source, validate_file, validate_many
from .service import AnalysisRequest, AnalysisResult, analyze_dataset
from .workspace import Workspace, WorkspaceError, assert_not_sample, uploads_root

__all__ = [
    "AnalysisRequest", "AnalysisResult", "FileValidation", "Workspace", "WorkspaceError",
    "analyze_dataset", "assert_not_sample", "detect_source", "uploads_root",
    "validate_file", "validate_many",
]
