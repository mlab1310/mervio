"""Ecriture des livrables sur disque.

Trois fichiers par analyse:
  report.json        format machine, contrat de sortie du moteur
  report.txt         lisible par un dirigeant
  data_quality.json  validations, couverture, incidents, limites
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List

from ..application.service import AnalysisResult
from ..application.workspace import assert_not_sample
from ..logging_config import get_logger
from .executive import render_executive_report

log = get_logger("reporting.writers")

REPORT_JSON = "report.json"
REPORT_TXT = "report.txt"
DATA_QUALITY_JSON = "data_quality.json"


def write_outputs(result: AnalysisResult, out_dir: str | Path) -> List[Path]:
    """Ecrit les 3 livrables. Retourne les chemins ecrits."""
    if not result.succeeded:
        raise ValueError("analyse non aboutie: aucun rapport a ecrire")
    directory = Path(out_dir)
    assert_not_sample(directory)  # jamais de livrable client dans les fixtures
    directory.mkdir(parents=True, exist_ok=True)

    written: List[Path] = []

    json_path = directory / REPORT_JSON
    json_path.write_text(json.dumps(result.report, indent=2, ensure_ascii=False), encoding="utf-8")
    written.append(json_path)

    txt_path = directory / REPORT_TXT
    txt_path.write_text(render_executive_report(result.report), encoding="utf-8")
    written.append(txt_path)

    quality_path = directory / DATA_QUALITY_JSON
    quality_path.write_text(
        json.dumps(result.data_quality_document(), indent=2, ensure_ascii=False), encoding="utf-8"
    )
    written.append(quality_path)

    result.written_files = [str(p) for p in written]
    log.info("analyse %s: %s fichiers ecrits dans %s", result.analysis_id, len(written), directory)
    return written
