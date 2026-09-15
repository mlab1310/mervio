"""Espace de travail des fichiers importes.

Regle de securite: les fichiers d'un client reel ne doivent JAMAIS atterrir
dans data/sample/, qui est reserve aux fixtures synthetiques versionnees.
Les imports vont dans data/uploads/<workspace_id>/, ignore par Git.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from ..errors import MervioError

#: repertoire des fixtures synthetiques, interdit aux donnees client
SAMPLE_DIRNAME = "sample"
UPLOAD_DIRNAME = "uploads"


class WorkspaceError(MervioError):
    """Operation interdite sur l'espace de travail."""


def project_root() -> Path:
    # src/mervio/application/workspace.py -> remonte a la racine du projet
    return Path(__file__).resolve().parents[3]


def uploads_root(base: Optional[Path] = None) -> Path:
    return (base or project_root()) / "data" / UPLOAD_DIRNAME


def is_sample_path(path: Path) -> bool:
    return SAMPLE_DIRNAME in {part.lower() for part in Path(path).resolve().parts}


@dataclass
class Workspace:
    """Repertoire isole pour une session d'import."""

    workspace_id: str
    root: Path
    created_at: datetime
    files: List[Path] = field(default_factory=list)

    @classmethod
    def create(cls, base: Optional[Path] = None, workspace_id: Optional[str] = None) -> "Workspace":
        wid = workspace_id or f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        root = uploads_root(base) / wid
        root.mkdir(parents=True, exist_ok=True)
        (root / ".gitignore").write_text("*\n", encoding="utf-8")
        return cls(workspace_id=wid, root=root, created_at=datetime.now(timezone.utc))

    def add_file(self, source_path: str | Path, name: Optional[str] = None) -> Path:
        origin = Path(source_path)
        if not origin.exists():
            raise WorkspaceError(f"fichier introuvable: {origin}")
        target = self.root / (name or origin.name)
        shutil.copy2(origin, target)
        self.files.append(target)
        return target

    def cleanup(self) -> None:
        """Supprime les fichiers importes. A appeler apres analyse si la
        donnee client ne doit pas persister sur la machine."""
        if self.root.exists() and UPLOAD_DIRNAME in self.root.parts:
            shutil.rmtree(self.root, ignore_errors=True)
        self.files.clear()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc) -> None:
        return None


def assert_not_sample(path: str | Path) -> None:
    """Garde-fou: interdit d'ecrire une donnee client dans data/sample/."""
    if is_sample_path(path):
        raise WorkspaceError(
            "data/sample/ est reserve aux fixtures synthetiques: "
            "une donnee client ne doit jamais y etre ecrite"
        )
