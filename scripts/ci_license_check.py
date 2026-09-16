#!/usr/bin/env python3
"""Porte CI: licences des dependances installees depuis `requirements.lock` (Mission 004.3).

    python scripts/ci_license_check.py requirements.lock

Responsabilite unique: pour chaque ligne du lock applicable a la plateforme, lire la licence
declaree par la distribution INSTALLEE (`License-Expression`, sinon `License`, sinon les
classifieurs) et refuser:

- une distribution du lock absente de l'environnement;
- une licence absente ou non reconnue (on ne devine pas);
- une licence hors de la liste d'autorisation ci-dessous.

Expressions SPDX simples prises en charge: `A`, `A OR B` (une option autorisee suffit),
`A AND B` (toutes doivent l'etre). Une expression mixte ou parenthesee est refusee: elle
exige une revue humaine et une entree explicite dans `REVIEWED`.

Codes de sortie: 0 porte franchie, 1 porte refusee, 2 usage invalide.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

#: Licences permissives acceptees sans revue.
PERMISSIVE = frozenset({"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "PSF-2.0", "ISC"})

#: Licences acceptees APRES revue, pour une distribution nommee et une raison ecrite.
#: psycopg: LGPL-3.0, utilise comme bibliotheque dynamique non modifiee; aucune obligation
#: de publication du code de Mervio (ADR-004.1-001).
REVIEWED: Dict[str, frozenset] = {
    "psycopg": frozenset({"LGPL-3.0-only"}),
    "psycopg-binary": frozenset({"LGPL-3.0-only"}),
}

#: Champ `License` libre ou classifieur -> identifiant SPDX, uniquement pour les formes non ambigues.
ALIASES = {
    "MIT": "MIT",
    "MIT License": "MIT",
    "License :: OSI Approved :: MIT License": "MIT",
    "Apache Software License": "Apache-2.0",
    "Apache 2.0": "Apache-2.0",
    "Apache-2.0": "Apache-2.0",
    "License :: OSI Approved :: Apache Software License": "Apache-2.0",
    "BSD-3-Clause": "BSD-3-Clause",
    "BSD-2-Clause": "BSD-2-Clause",
    "ISC": "ISC",
    "License :: OSI Approved :: ISC License (ISCL)": "ISC",
    "License :: OSI Approved :: Python Software Foundation License": "PSF-2.0",
}


@dataclass(frozen=True)
class Verdict:
    name: str
    version: Optional[str]
    license: Optional[str]
    problem: Optional[str]


def normalize_name(name: str) -> str:
    import re
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_license(meta) -> Optional[str]:
    """Licence declaree, SPDX si possible. None si rien d'exploitable."""
    expression = (meta.get("License-Expression") or "").strip()
    if expression:
        return expression
    field = " ".join((meta.get("License") or "").split())
    if field and len(field) <= 60:
        return ALIASES.get(field, field)
    classifiers = sorted({ALIASES.get(c, c) for c in (meta.get_all("Classifier") or [])
                          if c.startswith("License ::")})
    if len(classifiers) == 1:
        return classifiers[0]
    return None


def license_problem(name: str, expression: Optional[str]) -> Optional[str]:
    """Raison de refus, ou None si la licence est acceptable pour cette distribution."""
    if not expression:
        return "licence absente ou illisible"
    allowed = PERMISSIVE | REVIEWED.get(normalize_name(name), frozenset())
    if "(" in expression or ")" in expression or (" OR " in expression and " AND " in expression):
        return f"expression de licence a revoir: {expression}"
    if " OR " in expression:
        options = [part.strip() for part in expression.split(" OR ")]
        return None if any(option in allowed for option in options) else f"licence non autorisee: {expression}"
    parts = [part.strip() for part in expression.split(" AND ")]
    refused = [part for part in parts if part not in allowed]
    return f"licence non autorisee: {', '.join(refused)}" if refused else None


def locked_distributions(lock: Path) -> List[str]:
    """Noms des lignes du lock applicables a la plateforme courante."""
    from packaging.requirements import Requirement
    names = []
    for raw in lock.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""}):
            names.append(requirement.name)
    return names


def check(names: Sequence[str], lookup: Optional[Callable[[str], object]] = None) -> List[Verdict]:
    lookup = lookup or metadata.metadata
    verdicts = []
    for name in names:
        try:
            meta = lookup(name)
        except metadata.PackageNotFoundError:
            verdicts.append(Verdict(name, None, None, "distribution du lock non installee"))
            continue
        expression = declared_license(meta)
        verdicts.append(Verdict(name, meta.get("Version"), expression, license_problem(name, expression)))
    return verdicts


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: ci_license_check.py requirements.lock", file=sys.stderr)
        return 2
    lock = Path(args[0])
    if not lock.is_file():
        print(f"ci_license_check: fichier introuvable: {lock.name}", file=sys.stderr)
        return 2
    names = locked_distributions(lock)
    if not names:
        print("ci_license_check: lock vide", file=sys.stderr)
        return 2
    verdicts = check(names)
    for verdict in verdicts:
        status = "REFUS" if verdict.problem else "ok"
        print(f"{status:5} {verdict.name}=={verdict.version or '?'}  {verdict.license or '-'}"
              + (f"  ({verdict.problem})" if verdict.problem else ""))
    if any(verdict.problem for verdict in verdicts):
        return 1
    print(f"ci_license_check: {len(verdicts)} distributions, porte franchie")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
