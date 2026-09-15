"""Hygiene des textes non fiables et detection de fuites.

Tout texte issu d'une source importee (titre produit, nom de campagne, note,
message d'incident qui cite une valeur) est une DONNEE NON FIABLE. Ce module:

- neutralise ce qui pourrait brouiller la frontiere donnees / instructions
  (caracteres de controle, marqueurs bidirectionnels, balises d'enveloppe);
- masque les donnees personnelles et les secrets avant tout envoi au modele;
- signale les textes qui ressemblent a une instruction, sans les supprimer:
  la defense principale est la separation des canaux (voir prompt.py), le
  signalement n'est qu'une information supplementaire.

Les motifs de secrets sont ceux de la detection a l'import (D-017, D-018).
"""
from __future__ import annotations

import re
from typing import List

from ..application.sensitive import _CARD_CANDIDATE, _PATTERNS, _luhn
from ..ingestion.base import _EMAIL_RE

#: caracteres de controle, marqueurs bidirectionnels et caracteres invisibles
_INVISIBLE_RE = re.compile("[\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff]")
_PHONE_RE = re.compile(r"(?<![\w+])(?:\+\d{1,3}[ .-]?\d(?:[ .-]?\d{2,4}){2,5}|0\d(?:[ .-]?\d{2}){4})(?!\w)")
#: identifiant de commande Shopify (#1001) ou client invite (guest:#1001)
_ORDER_ID_RE = re.compile(r"(?:guest:)?(?<![\w&])#\d{3,}\b")

_INSTRUCTION_MARKERS = re.compile(
    r"ignore[rz]?\b.{0,40}\b(instructions?|rules?|regles?|consignes?)"
    r"|disregard|oublie[rz]?\b.{0,30}\b(instructions?|regles?|consignes?)"
    r"|system\s*prompt|prompt\s*syst[eè]me|reveal|r[eé]v[eè]le"
    r"|api[\s_-]*key|cl[eé]\s*api|\bact\s+as\b|you\s+are\s+now|tu\s+es\s+maintenant"
    r"|jailbreak|developer\s+mode|administrat(or|eur)"
    r"|</?\s*(system|assistant|user)\s*>|\b(system|assistant)\s*:"
    r"|change[rz]?\b.{0,30}\bscore|calcul(ate|e)[rz]?\b.{0,30}\b(profit|revenue|ca\b|marge)"
    r"|MERVIO_UNTRUSTED_DATA",
    re.IGNORECASE,
)


def sanitize_text(value: object, limit: int) -> str:
    """Rend un texte non fiable transportable dans l'enveloppe de donnees.

    Le sens est preserve (la donnee reste une donnee), seuls les elements
    dangereux ou personnels sont masques, puis le texte est tronque.
    """
    # bornes AVANT tout traitement: un champ de plusieurs megaoctets ne doit
    # pas couter plus cher qu'un champ normal. Apres une coupe, le dernier mot,
    # peut-etre tronque au milieu d'un email ou d'un IBAN, est ecarte pour ne
    # pas echapper au masquage sous une forme partielle.
    raw = "" if value is None else str(value)
    hard_cap = limit * 8 + 256
    cut = len(raw) > hard_cap
    text = " ".join(_INVISIBLE_RE.sub(" ", raw[:hard_cap]).split())
    window = limit * 2 + 64
    if len(text) > window:
        text, cut = text[:window], True
    if cut:
        text = text.rsplit(" ", 1)[0] if " " in text else ""
    text = redact_sensitive(text)
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def redact_sensitive(text: str) -> str:
    text = _EMAIL_RE.sub("[email masque]", text)
    for kind, pattern in _PATTERNS.items():
        text = pattern.sub(f"[{kind} masque]", text)
    text = _CARD_CANDIDATE.sub(_mask_card, text)
    text = _PHONE_RE.sub("[telephone masque]", text)
    return _ORDER_ID_RE.sub("[commande]", text)


def _mask_card(match: "re.Match[str]") -> str:
    digits = re.sub(r"[ -]", "", match.group(0))
    return "[carte masquee]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


def looks_like_instruction(text: str) -> bool:
    return bool(_INSTRUCTION_MARKERS.search(text))


def sensitive_findings(text: str) -> List[str]:
    """Types de donnees sensibles presentes dans un texte. Jamais la valeur."""
    kinds = []
    if _EMAIL_RE.search(text):
        kinds.append("email")
    kinds.extend(kind for kind, pattern in _PATTERNS.items() if pattern.search(text))
    if any(_mask_card(m) != m.group(0) for m in _CARD_CANDIDATE.finditer(text)):
        kinds.append("card_number")
    if _PHONE_RE.search(text):
        kinds.append("phone")
    return kinds
