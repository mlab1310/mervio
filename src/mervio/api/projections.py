"""Du rapport PERSISTE au corps de reponse (Mission 004.6, tranche G-1).

AUCUNE ROUTE ICI. Ce module ne contient que des fonctions pures: un rapport persiste en
entree, des octets et un ETag en sortie. Rien n'est enregistre aupres de l'application, donc
aucune surface HTTP n'existe avant G-2. Les quatre gardes structurelles de
`tests/api/test_api_guards.py` s'appliquent des maintenant.

LE CORPS EST LES OCTETS PERSISTES, PAS UNE RESERIALISATION
`reports.payload_json` est le texte EXACT produit par `serialize_report` au moment de
l'analyse, et `reports` est immuable (`reports_forbid_update`, revision 0003). Le servir
consiste donc a l'encoder en UTF-8, et a RIEN d'autre. Relire en `dict` puis re-serialiser
changerait l'ordre des clefs, les separateurs ou l'echappement: les octets ne seraient plus
ceux que la persistance a scelles, l'empreinte ne correspondrait plus, et un ETag calcule sur
le resultat designerait un document que la base ne contient pas. Aucun `json.dumps` n'entre
donc ici, et un test structurel le verrouille.

L'ETAG EST L'EMPREINTE PERSISTEE, APRES VERIFICATION
`payload_sha256` est ecrit en meme temps que la charge utile, par la meme transaction. Il est
donc deja l'empreinte du contenu; le recalculer a chaque lecture serait du travail inutile.
Mais le croire sur parole serait servir un ETag qui n'engage rien: l'empreinte est donc
RECALCULEE et COMPAREE, et une divergence leve `ReportIntegrityError`. Ce n'est pas une erreur
du client -- il n'y est pour rien -- donc elle n'est traduite par aucun gestionnaire de G-0 et
reste un 500. Un octet modifie sous une table pourtant immuable est un defaut interne.

LES CONSTATS VIVENT DANS QUATRE SECTIONS, ET IL N'Y A PAS DE CLE `findings`
Le contrat de rapport est explicite: << il n'existe ni cle `findings`, ni cle `evidence` a la
racine >>. Les constats sont repartis dans `critical_issues`, `warnings`, `setup` et
`opportunities`, DANS CET ORDRE (AN-18 / D-084). `derive_findings` les reunit en respectant cet
ordre, sans jamais modifier le rapport recu.

L'ORDRE EST RECOPIE DU MOTEUR, ET UN TEST LE PROUVE
`SECTIONS` ci-dessous duplique l'ordre que `analytics/insights.py` possede. La duplication est
IMPOSEE: la garde 1 interdit a `api/` d'importer le moteur analytique. Elle n'est donc pas une
negligence, et `test_the_section_order_matches_the_engine` compare les deux -- un test peut
importer les deux cotes, un module de `api/` ne le peut pas.

UN RAPPORT 1.x RESTE SERVI, ET C'EST UN ENGAGEMENT
`setup` est la quatrieme section, ajoutee en 2.0 (AN-18 / D-084). Un rapport 1.x n'a pas cette
clef et doit rester lisible (ND-04). Elle est donc lue avec tolerance, comme le fait deja
`reporting/executive.py`, tandis que les trois sections presentes depuis 1.0 sont exigees. Le
schema 2.0 refuserait un rapport 1.x: c'est precisement pourquoi la garde 2 interdit de
l'importer ici.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, List, Mapping, Tuple

from ..errors import MervioError

#: sections de constat, DANS L'ORDRE de lecture ratifie (AN-18 / D-084), et la categorie que
#: chacune porte quand le constat ne la declare pas lui-meme. Recopie du moteur, que la
#: garde 1 interdit d'importer; `test_the_section_order_matches_the_engine` compare les deux.
SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("critical_issues", "critical_issue"),
    ("warnings", "warning"),
    ("setup", "setup"),
    ("opportunities", "opportunity"),
)

#: sections absentes d'un rapport 1.x, donc lues avec tolerance. Les autres sont EXIGEES: leur
#: absence est un rapport casse, pas un rapport ancien.
OPTIONAL_SECTIONS = frozenset({"setup"})


class ReportProjectionError(MervioError):
    """Le rapport persiste n'a pas la forme attendue. Defaut INTERNE, jamais une faute client."""


class ReportIntegrityError(ReportProjectionError):
    """Les octets persistes ne correspondent pas a leur empreinte persistee."""


@dataclass(frozen=True)
class ProjectedReport:
    """Ce qu'une route a besoin de servir, et rien de plus."""

    body: bytes
    etag: str

    @property
    def length(self) -> int:
        return len(self.body)


def report_body(payload_json: str) -> bytes:
    """Les octets a servir: l'encodage UTF-8 du texte persiste, sans aucune transformation."""
    return payload_json.encode("utf-8")


def report_etag(payload_json: str, *, payload_sha256: str) -> str:
    """ETag FORT, derive du contenu persiste et de son empreinte VERIFIEE.

    Fort, et non faible: deux reponses portant cet ETag sont identiques octet pour octet,
    puisqu'il est l'empreinte de ces octets memes.
    """
    observed = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if observed != payload_sha256:
        # Aucune valeur n'est reproduite dans le message: ni l'empreinte attendue, ni celle
        # observee. L'incident se diagnostique en base, pas dans une trace d'exception.
        raise ReportIntegrityError("empreinte du rapport persiste incoherente")
    return f'"{observed}"'


def project_report(stored) -> ProjectedReport:
    """Corps et ETag d'un `persistence.analyses.StoredReport`.

    Le parametre n'est pas type pour ne pas imposer un import a ce module: seuls
    `payload_json` et `record.payload_sha256` sont lus.
    """
    payload_json = stored.payload_json
    return ProjectedReport(body=report_body(payload_json),
                           etag=report_etag(payload_json,
                                            payload_sha256=stored.record.payload_sha256))


def etag_matches(etag: str, header: str) -> bool:
    """Vrai si `If-None-Match` designe cette representation (RFC 9110, section 13.1.2).

    `*` designe toute representation existante. Une liste est separee par des virgules. Un
    ETag FAIBLE (`W/"..."`) correspond ici, parce que la comparaison de `If-None-Match` est
    faible par definition -- c'est `If-Match` qui exige une comparaison forte.
    """
    for candidate in (part.strip() for part in header.split(",")):
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def derive_findings(report: Mapping[str, Any]) -> List[dict]:
    """Les constats des quatre sections, reunis DANS L'ORDRE ratifie (AN-18 / D-084).

    Le rapport recu n'est JAMAIS modifie, et le resultat n'en PARTAGE aucun conteneur: un
    consommateur peut ecrire dans un constat projete sans que rien n'atteigne le rapport.
    La categorie n'est posee que si le constat n'en declare pas deja une.
    """
    findings: List[dict] = []
    for section, category in SECTIONS:
        for item in _section(report, section):
            if not isinstance(item, Mapping):
                raise ReportProjectionError(f"constat non structure dans {section}")
            projected = _detached(item)
            projected.setdefault("category", category)
            findings.append(projected)
    return findings


def _detached(value: Any) -> Any:
    """Copie d'une valeur JSON: les conteneurs sont recopies, les scalaires partages.

    POURQUOI UNE COPIE DE SURFACE NE SUFFISAIT PAS, ET LA MESURE QUI L'A MONTRE
    Un constat 2.0 porte des objets IMBRIQUES: `evidence` est une liste de faits structures
    (AN-15 / D-082), `confidence` vaut `{level, criteria}` (AN-14 / D-081), et les `_params`
    sont des objets (AN-19 / D-085). Avec `dict(item)`, ces sous-objets restaient les MEMES
    que ceux du rapport: mesure sur un constat de reference, ecrire
    `constat["confidence"]["level"]` changeait le rapport source, et `evidence` comme
    `confidence` etaient le meme objet des deux cotes. Or `reports` est immuable et le meme
    rapport sert de source a d'autres lectures de la meme requete.

    CE N'EST PAS `copy.deepcopy`, ET C'EST DELIBERE
    Le rapport vient de `json.loads`: les seuls types admis sont objet, tableau, chaine,
    nombre, booleen et nul. Les quatre derniers sont IMMUABLES, donc les partager ne cree
    aucun alias par lequel ecrire. Il ne reste que deux conteneurs a recopier, sans cycle a
    suivre, sans protocole a honorer et sans classe inconnue a reconstruire -- une copie
    profonde generique paierait tout cela pour rien.
    """
    if isinstance(value, Mapping):
        return {key: _detached(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_detached(item) for item in value]
    return value


def _section(report: Mapping[str, Any], section: str) -> List[Any]:
    """Contenu d'une section. Absente et vide sont distinguees, et seule `setup` peut manquer."""
    if section not in report:
        if section in OPTIONAL_SECTIONS:
            return []                                # rapport 1.x: la section n'existait pas
        raise ReportProjectionError(f"rapport sans section {section}")
    items = report[section]
    if items is None:
        return []
    if not isinstance(items, list):
        raise ReportProjectionError(f"section {section} n'est pas une liste")
    return items
