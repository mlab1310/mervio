"""Montants: float du domaine <-> numeric(19,4) PostgreSQL, sans perte.

Le domaine represente les montants en float (models.py). La base les stocke en
numeric(19,4): jamais de float pour un montant autoritaire.

Garantie d'aller-retour exact: les connecteurs lisent un montant depuis un
texte decimal ("89.99") et produisent le float le plus proche. repr(float) en
redonne le texte decimal le plus court; s'il tient en 4 decimales et 15 chiffres
entiers, numeric(19,4) le stocke exactement, et float(texte relu) redonne le
MEME float (arrondi correct dans les deux sens). Tout autre montant est REFUSE
(MoneyPrecisionError), jamais arrondi en silence:
- plus de 4 decimales, ou |montant| >= 10^15;
- non fini (inf, nan);
- zero negatif (-0.0): numeric n'a pas de zero signe;
- type autre que float (un int relu deviendrait float et changerait la serialisation).
"""
from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from typing import Optional

from .errors import MoneyPrecisionError

MONEY_PRECISION = 19
MONEY_SCALE = 4
_MAX_INTEGER_DIGITS = MONEY_PRECISION - MONEY_SCALE
_QUANTUM = Decimal(1).scaleb(-MONEY_SCALE)
_LIMIT = Decimal(10) ** _MAX_INTEGER_DIGITS


def money_to_db(value: float, *, field: str) -> str:
    """Texte decimal exact a envoyer en numeric(19,4). Leve MoneyPrecisionError sinon."""
    if type(value) is not float:
        raise MoneyPrecisionError(f"{field}: montant de type {type(value).__name__}, float attendu")
    if not math.isfinite(value):
        raise MoneyPrecisionError(f"{field}: montant non fini")
    if value == 0.0 and math.copysign(1.0, value) < 0:
        raise MoneyPrecisionError(f"{field}: zero negatif non representable en numeric")
    text = repr(value)
    # chemin rapide: forme decimale simple, sans exposant
    if "e" not in text:
        integer, _, decimals = text.lstrip("-").partition(".")
        if decimals == "0":
            decimals = ""
        if len(decimals.rstrip("0")) <= MONEY_SCALE and len(integer.lstrip("0")) <= _MAX_INTEGER_DIGITS:
            return text
        raise MoneyPrecisionError(
            f"{field}: montant non representable exactement en numeric({MONEY_PRECISION},{MONEY_SCALE})")
    try:
        exact = Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - repr d'un float fini est toujours decimal
        raise MoneyPrecisionError(f"{field}: montant illisible") from exc
    if exact != exact.quantize(_QUANTUM) or abs(exact) >= _LIMIT:
        raise MoneyPrecisionError(
            f"{field}: montant non representable exactement en numeric({MONEY_PRECISION},{MONEY_SCALE})")
    return format(exact, "f")


def optional_money_to_db(value: Optional[float], *, field: str) -> Optional[str]:
    return None if value is None else money_to_db(value, field=field)


def money_from_db(value) -> float:
    """numeric relu (Decimal ou texte) -> float du domaine."""
    return float(value)
