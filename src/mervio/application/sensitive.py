"""Detection de donnees sensibles dans un fichier importe.

Objectif: prevenir l'utilisateur AVANT l'analyse qu'un export contient des
elements qui n'ont rien a faire dans un CSV d'analytics (numero de carte, cle
API, IBAN, jeton). Mervio n'a besoin d'aucune de ces donnees.

Regle absolue de ce module: il signale la COLONNE et le NOMBRE d'occurrences.
Il ne renvoie, ne journalise et n'ecrit JAMAIS la valeur detectee.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List

#: motifs de secrets. Volontairement conservateurs pour limiter les faux positifs.
_PATTERNS = {
    "api_key": re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{8,}\b"),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}\b", re.IGNORECASE),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){13,19}\b")

#: colonnes dont le nom seul suffit a alerter
_SUSPICIOUS_COLUMNS = {
    "card number", "credit card", "card_number", "cvv", "cvc", "iban", "bic",
    "password", "api key", "api_key", "secret", "token", "access_token",
    "social security", "numero de carte",
}

#: nombre de lignes inspectees (un scan complet serait inutilement couteux)
SCAN_ROWS = 500


@dataclass
class SensitiveFinding:
    kind: str
    column: str
    occurrences: int

    def to_dict(self) -> dict:
        return {"kind": self.kind, "column": self.column, "occurrences": self.occurrences}

    def message(self) -> str:
        return (f"donnee sensible probable ({self.kind}) dans la colonne '{self.column}' "
                f"sur {self.occurrences} ligne(s): Mervio n'en a pas besoin, "
                f"supprimez cette colonne avant l'import")


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for index, char in enumerate(digits):
        value = int(char)
        if index % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def scan_rows(rows: Iterable[dict], limit: int = SCAN_ROWS) -> List[SensitiveFinding]:
    """Inspecte un echantillon de lignes. Ne retourne jamais de valeur."""
    counts: Dict[tuple, int] = {}
    rows = list(rows)[:limit]

    for column in (rows[0].keys() if rows else []):
        if column.strip().lower() in _SUSPICIOUS_COLUMNS:
            counts[("suspicious_column", column)] = counts.get(("suspicious_column", column), 0) + 1

    for row in rows:
        for column, value in row.items():
            if not isinstance(value, str) or not value:
                continue
            for kind, pattern in _PATTERNS.items():
                if pattern.search(value):
                    counts[(kind, column)] = counts.get((kind, column), 0) + 1
            for candidate in _CARD_CANDIDATE.findall(value):
                digits = re.sub(r"[ -]", "", candidate)
                if 13 <= len(digits) <= 19 and _luhn(digits):
                    counts[("card_number", column)] = counts.get(("card_number", column), 0) + 1
                    break

    return [SensitiveFinding(kind, column, count) for (kind, column), count in sorted(counts.items())]
