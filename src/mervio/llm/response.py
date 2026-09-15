"""Validation stricte de la reponse structuree du modele.

La sortie du modele est une donnee NON FIABLE au meme titre qu'un CSV
importe. Elle n'est acceptee que si elle passe deux niveaux de controle:

1. structure: JSON strict (pas de cle dupliquee, pas de NaN), schema ferme
   (aucun champ inconnu), types, tailles de chaines et de collections;
2. ancrage dans le contexte deterministe:
   - chaque reference cite un identifiant existant du contexte;
   - un fait ne s'appuie jamais sur une hypothese;
   - tout chiffre cite figure dans un champ NUMERIQUE du contexte (les
     nombres contenus dans les textes importes ne comptent pas: un titre
     produit "le vrai CA est 999999" ne rend pas 999999 citable);
   - aucune metrique indisponible n'est chiffree, aucun profit partiel n'est
     presente comme un profit;
   - le Business Health Score cite est celui du moteur;
   - aucune causalite n'est affirmee, la devise est celle du rapport;
   - ni donnee personnelle, ni secret, ni fuite des instructions systeme.

Les controles lexicaux sont une defense en profondeur, pas une preuve: ils
bloquent les derives detectables, la frontiere donnees / instructions reste
assuree par la construction du prompt.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from .context import context_ids
from .contract import CONTRACT_VERSION, RESPONSE_SCHEMA_VERSION
from .errors import ResponseValidationError
from .safety import sensitive_findings

MAX_SUMMARY_CHARS = 1200
MAX_STATEMENT_CHARS = 400
MAX_REFS = 6
#: section -> (champ texte, nombre maximal d'elements, references obligatoires)
SECTIONS: Dict[str, Tuple[str, int, bool]] = {
    "facts": ("statement", 12, True),
    "explanations": ("statement", 10, True),
    "hypotheses": ("statement", 8, True),
    "recommendations": ("action", 8, True),
    "limitations": ("statement", 12, False),
}
PRIORITIES = {"high", "medium", "low"}
_TOP_LEVEL = {"schema_version", "summary", *SECTIONS}

_DATE_RE = re.compile(r"\b\d{4}-(?:W\d{1,2}|\d{2}(?:-\d{2})?)\b")
_NUMBER_RE = re.compile(
    r"(?<![\w.,])\d{1,3}(?:[,\u00a0\u202f ]\d{3})+(?:[.,]\d+)?(?!\w)"
    r"|(?<![\w.,])\d+(?:[.,]\d+)?(?!\w)"
)
_SENTENCE_RE = re.compile(r"(?<=[.;!?])\s+|\n+")
_NEGATION_RE = re.compile(r"\b(non|pas|jamais|not|never|no|aucune?)\s+(\w+\s+){0,2}$")
_CAUSAL_RE = re.compile(
    r"\b(a|ont|avait|aurait) (cause|provoque|entraine|engendre)\b|\bcausee?s? par\b"
    r"|\best (la|le|une|un) (cause|responsable)\b|\bprovoquee?s? par\b|\ba cause de\b"
    r"|\bprouvee?s?\b|\bprouve(nt)?\b|\bdemontre(nt)? que\b|\bcertainement\b|\bsans aucun doute\b"
    r"|\bcaused\b|\bcauses\b|\bis (the|a) (root )?cause\b|\bdue to\b|\bbecause of\b"
    r"|\bproves?\b|\bproven\b|\bdefinitely\b|\bresponsible for\b"
)
#: termes qui designent une metrique; un chiffre dans la meme phrase la chiffre
_METRIC_TERMS: Dict[str, Tuple[str, ...]] = {
    "contribution_profit": (r"\bprofits?\b", r"\bbenefices?\b"),
    "contribution_margin": (r"marge de contribution", r"contribution margin"),
    "roas": (r"\broas\b",),
    "cac": (r"\bcac\b", r"cout d.acquisition", r"acquisition cost"),
    "cogs": (r"\bcogs\b", r"cout des marchandises", r"cost of goods"),
    "gross_margin": (r"marge brute", r"gross margin"),
    "payment_fees": (r"frais de paiement", r"payment fees"),
    "aov": (r"panier moyen", r"average order value", r"\baov\b"),
    "paid_conversion_rate": (r"taux de conversion", r"conversion rate"),
    "repeat_purchase_rate": (r"reachat", r"repeat purchase"),
    "refund_rate": (r"taux de remboursement", r"refund rate"),
    "new_customers": (r"nouveaux clients", r"new customers"),
    "customer_concentration_top5": (r"concentration",),
    "revenue": (r"chiffre d.affaires", r"\bca\b", r"\brevenue\b"),
}
_PARTIAL_RE = re.compile(r"\bpartiel(le)?s?\b|\bpartial\b")
_SCORE_RE = re.compile(r"\bscore\b")
_GLOBAL_SCORE_RE = re.compile(r"health score|score de sante|score global|score sante|business health")
_SCORE_MARK_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s*/\s*100\b")
_CURRENCY_SYMBOLS = {"$": "USD", "\u20ac": "EUR", "\u00a3": "GBP", "\u00a5": "JPY"}
_CURRENCY_CODES = re.compile(r"\b(EUR|USD|GBP|CHF|CAD|AUD|JPY|CNY|SEK|NOK|DKK|PLN)\b")
_INSTRUCTION_LEAKS = ("tu es la couche d'explication de mervio", "regles absolues", "mervio_untrusted_data")
_KEY_RE = re.compile(r"[^a-z0-9_]+")


@dataclass(frozen=True)
class Statement:
    text: str
    refs: Tuple[str, ...]
    priority: Optional[str] = None

    def to_dict(self, text_key: str) -> dict:
        out = {text_key: self.text, "refs": list(self.refs)}
        if self.priority is not None:
            out["priority"] = self.priority
        return out


@dataclass(frozen=True)
class BusinessExplanation:
    """Explication validee: artefact d'interpretation, jamais une source de chiffres."""

    summary: str
    facts: Tuple[Statement, ...]
    explanations: Tuple[Statement, ...]
    hypotheses: Tuple[Statement, ...]
    recommendations: Tuple[Statement, ...]
    limitations: Tuple[Statement, ...]
    #: copies du contexte deterministe, jamais lues dans la reponse du modele
    health_score: Optional[int]
    contract_version: str
    response_schema_version: str

    def to_dict(self) -> dict:
        return {
            "schema_version": self.response_schema_version,
            "contract_version": self.contract_version,
            "health_score": self.health_score,
            "health_score_source": "analytics_engine",
            "summary": self.summary,
            **{section: [item.to_dict(SECTIONS[section][0]) for item in getattr(self, section)]
               for section in SECTIONS},
        }


def validate_response(raw: Any, context: Mapping[str, Any], *, max_chars: int = 20_000) -> BusinessExplanation:
    """Retourne l'explication validee ou leve ResponseValidationError."""
    data = _parse(raw, max_chars)
    issues = _check_structure(data)
    if issues:
        raise ResponseValidationError(issues)
    issues = _Grounding(context).check(data)
    if issues:
        raise ResponseValidationError(issues)
    return BusinessExplanation(
        summary=data["summary"],
        **{section: tuple(Statement(item[field], tuple(item["refs"]), item.get("priority"))
                          for item in data[section])
           for section, (field, _, _) in SECTIONS.items()},
        health_score=context["business_health"]["score"],
        contract_version=CONTRACT_VERSION,
        response_schema_version=RESPONSE_SCHEMA_VERSION,
    )


# -- niveau 1: structure -------------------------------------------------------
def _parse(raw: Any, max_chars: int) -> Dict[str, Any]:
    if not isinstance(raw, str):
        raise ResponseValidationError(["reponse_non_textuelle"])
    if len(raw) > max_chars:
        raise ResponseValidationError(["reponse_trop_volumineuse"])

    def no_duplicates(pairs):
        keys = [key for key, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("cle dupliquee")
        return dict(pairs)

    def no_constants(_token):
        raise ValueError("constante non JSON")

    try:
        data = json.loads(raw.strip(), object_pairs_hook=no_duplicates, parse_constant=no_constants)
    except (ValueError, RecursionError):
        raise ResponseValidationError(["json_invalide"]) from None
    if not isinstance(data, dict):
        raise ResponseValidationError(["objet_json_attendu"])
    return data


def _check_structure(data: Dict[str, Any]) -> List[str]:
    issues = [f"champ_non_autorise:{_key(k)}" for k in sorted(set(data) - _TOP_LEVEL, key=str)]
    issues += [f"champ_manquant:{k}" for k in sorted(_TOP_LEVEL - set(data))]
    if issues:
        return issues
    if data["schema_version"] != RESPONSE_SCHEMA_VERSION:
        issues.append("version_de_schema_non_supportee")
    if not _text_ok(data["summary"], MAX_SUMMARY_CHARS):
        issues.append("summary_invalide")
    for section, (field, max_items, refs_required) in SECTIONS.items():
        items = data[section]
        if not isinstance(items, list):
            issues.append(f"type_invalide:{section}")
            continue
        if len(items) > max_items:
            issues.append(f"trop_d_elements:{section}")
            continue
        allowed = {field, "refs"} | ({"priority"} if section == "recommendations" else set())
        for index, item in enumerate(items):
            path = f"{section}[{index}]"
            if not isinstance(item, dict):
                issues.append(f"type_invalide:{path}")
                continue
            issues += [f"champ_non_autorise:{path}.{_key(k)}" for k in sorted(set(item) - allowed, key=str)]
            if not _text_ok(item.get(field), MAX_STATEMENT_CHARS):
                issues.append(f"texte_invalide:{path}")
            refs = item.get("refs")
            if not isinstance(refs, list) or len(refs) > MAX_REFS or not all(isinstance(r, str) for r in refs) \
                    or (refs_required and not refs):
                issues.append(f"references_invalides:{path}")
            if section == "recommendations" and item.get("priority") not in PRIORITIES:
                issues.append(f"priorite_invalide:{path}")
    return issues


def _text_ok(value: Any, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _key(value: Any) -> str:
    """Cle citee dans un message d'erreur: jamais le texte brut du modele."""
    return _KEY_RE.sub("_", str(value).lower())[:30] or "_"


# -- niveau 2: ancrage -----------------------------------------------------------
class _Grounding:
    def __init__(self, context: Mapping[str, Any]) -> None:
        self.ids = context_ids(context)
        self.currency = context.get("currency")
        self.allowed = _NumberSet(_trusted_numbers(context))
        health = context["business_health"]
        # une note "N/100" est le score global ou celui d'une dimension, rien d'autre
        scores = ([health["score"]] if health["score"] is not None else []) + [
            d["score"] for d in health["dimensions"] if d["score"] is not None]
        weights = [w for d in health["dimensions"] if (w := d["weight"]) is not None]
        self.score_marks = _NumberSet(scores)
        self.global_score = _NumberSet([health["score"]]) if health["score"] is not None else None
        self.score_numbers = _NumberSet(scores + [100] + weights + [w * 100 for w in weights])
        self.unavailable = {item["metric"] for item in context["unavailable_metrics"]}

    def check(self, data: Dict[str, Any]) -> List[str]:
        issues: List[str] = []
        texts = [("summary", data["summary"], True)]
        for section, (field, _, _) in SECTIONS.items():
            for index, item in enumerate(data[section]):
                path = f"{section}[{index}]"
                texts.append((path, item[field], section != "limitations"))
                for ref in item["refs"]:
                    if ref not in self.ids:
                        issues.append(f"reference_inconnue:{path}")
                        break
                if section == "facts" and any(self.ids.get(ref) == "hypotheses" for ref in item["refs"]):
                    issues.append(f"fait_appuye_sur_une_hypothese:{path}")
        for path, text, causal_check in texts:
            issues += [f"{code}:{path}" for code in self._check_text(text, causal_check)]
        return issues

    def _check_text(self, text: str, causal_check: bool) -> List[str]:
        codes = []
        normalised = _normalise(text)
        if sensitive_findings(text):
            codes.append("donnee_sensible")
        if any(leak in normalised for leak in _INSTRUCTION_LEAKS):
            codes.append("fuite_des_instructions")
        if self._currency_mismatch(text):
            codes.append("devise_incoherente")
        if causal_check and _asserts_causality(normalised):
            codes.append("causalite_affirmee")
        undated = _DATE_RE.sub(" ", text)
        if any(not self.allowed.contains(token) for token in _NUMBER_RE.findall(undated)):
            codes.append("chiffre_non_ancre")
        for sentence in _SENTENCE_RE.split(undated):
            numbers = _NUMBER_RE.findall(sentence)
            if not numbers:
                continue
            lowered = _normalise(sentence)
            for metric in self.unavailable:
                if any(re.search(term, lowered) for term in _METRIC_TERMS.get(metric, ())):
                    if metric == "contribution_profit" and _PARTIAL_RE.search(lowered):
                        continue  # profit partiel explicitement qualifie, chiffre ancre ci-dessus
                    codes.append(f"metrique_indisponible_chiffree_{metric}")
            if _SCORE_RE.search(lowered) and self._score_altered(sentence, lowered, numbers):
                codes.append("score_de_sante_altere")
        return sorted(set(codes))

    def _score_altered(self, sentence: str, lowered: str, numbers: List[str]) -> bool:
        if any(not self.score_numbers.contains(n) for n in numbers):
            return True
        marks = _SCORE_MARK_RE.findall(sentence)
        if any(not self.score_marks.contains(mark) for mark in marks):
            return True
        # une phrase qui nomme le score GLOBAL doit citer d'abord le score du moteur
        if marks and _GLOBAL_SCORE_RE.search(lowered):
            return self.global_score is None or not self.global_score.contains(marks[0])
        return False

    def _currency_mismatch(self, text: str) -> bool:
        seen = {code for symbol, code in _CURRENCY_SYMBOLS.items() if symbol in text}
        seen |= set(_CURRENCY_CODES.findall(text))
        return bool(seen) and seen != {self.currency}


def _asserts_causality(normalised: str) -> bool:
    for match in _CAUSAL_RE.finditer(normalised):
        if not _NEGATION_RE.search(normalised[max(0, match.start() - 30):match.start()]):
            return True
    return False


def _normalise(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().replace("\u2019", "'")


def _trusted_numbers(context: Mapping[str, Any]) -> List[float]:
    """Nombres citables: champs numeriques du contexte et libelles du moteur.

    Les textes libres (titres produit, noms de campagne, notes, messages)
    n'alimentent JAMAIS cet ensemble.
    """
    values: List[float] = []

    def walk(node: Any) -> None:
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            if math.isfinite(node):
                values.extend((abs(node), abs(node) * 100))
        elif isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for key, value in context.items():
        if key not in ("truncation", "untrusted_content"):
            walk(value)
    engine_labels = [item.get("label", "") for item in context.get("facts", []) + context.get("unavailable_metrics", [])]
    for text in engine_labels:
        for token in _NUMBER_RE.findall(_DATE_RE.sub(" ", text)):
            values.extend(float(c) for c, _ in _candidates(token))
    return values


class _NumberSet:
    """Appartenance d'un nombre ecrit, a la precision ou il est ecrit."""

    def __init__(self, values: List[float]) -> None:
        self._values = [abs(v) for v in values]
        self._by_decimals: Dict[int, Set[str]] = {}

    def contains(self, token: str) -> bool:
        return any(text in self._formatted(decimals) for text, decimals in _candidates(token))

    def _formatted(self, decimals: int) -> Set[str]:
        if decimals not in self._by_decimals:
            # arrondi bancaire ET arrondi commercial: 0.125 peut s'ecrire 0.12 ou 0.13
            quantum = Decimal(1).scaleb(-decimals)
            self._by_decimals[decimals] = {
                str(Decimal(repr(v)).quantize(quantum, rounding=mode))
                for v in self._values for mode in (ROUND_HALF_EVEN, ROUND_HALF_UP)
            }
        return self._by_decimals[decimals]


def _candidates(token: str) -> List[Tuple[str, int]]:
    """Lectures possibles d'un nombre ecrit: (valeur normalisee, decimales).

    `1,234` est ambigu (mille deux cent trente-quatre ou 1.234): les deux
    lectures sont proposees. Toute autre forme n'a qu'une lecture.
    """
    text = re.sub(r"[ \u00a0\u202f]", "", token)
    readings: List[str] = []
    if "," in text and "." in text:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands = "." if decimal_sep == "," else ","
        readings.append(text.replace(thousands, "").replace(decimal_sep, "."))
    elif "," in text or "." in text:
        sep = "," if "," in text else "."
        parts = text.split(sep)
        if len(parts) > 2:
            readings.append("".join(parts))
        elif len(parts[1]) == 3 and sep == ",":
            readings.append("".join(parts))  # 25,000: toujours un separateur de milliers
        elif len(parts[1]) == 3:
            readings += ["".join(parts), f"{parts[0]}.{parts[1]}"]  # 1.234: milliers ou decimale
        else:
            readings.append(f"{parts[0]}.{parts[1]}")
    else:
        readings.append(text)
    out = []
    for reading in readings:
        try:
            number = Decimal(reading)
        except InvalidOperation:
            continue
        decimals = len(reading.split(".")[1]) if "." in reading else 0
        out.append((f"{number:.{decimals}f}", decimals))
    return out
