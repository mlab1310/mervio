"""Projections pures du rapport persiste (Mission 004.6, tranche G-1).

AUCUNE ROUTE, AUCUNE BASE, AUCUN RESEAU. Ce module eprouve des fonctions: un rapport persiste
en entree, des octets, un ETag et des constats en sortie. La route qui les consommera est G-2.

CE QUI EST PROUVE ICI
Que le corps servi est le texte PERSISTE, et non une reserialisation -- la difference est
mesuree sur une charge utile deliberement non canonique. Que l'ETag est l'empreinte de ces
octets, verifiee et non crue sur parole. Que les constats sortent dans l'ordre ratifie
(AN-18 / D-084), le meme que celui du moteur, et qu'un rapport 1.x sans `setup` reste servi.
"""
from __future__ import annotations

import json

import pytest

from mervio.api.projections import (OPTIONAL_SECTIONS, SECTIONS, ProjectedReport,
                                    ReportIntegrityError, ReportProjectionError,
                                    derive_findings, etag_matches, project_report, report_body,
                                    report_etag)
from mervio.persistence.codec import serialize_report, sha256_text

REPORT = {
    "_meta": {"report_contract": "2.0", "currency": "EUR"},
    "executive_summary": "Chiffre d\'affaires en légère baisse",
    "critical_issues": [{"finding_id": "revenue_drop", "titre": "CA en baisse"}],
    "warnings": [{"finding_id": "margin"}],
    "setup": [{"finding_id": "profitability_unavailable"}],
    "opportunities": [{"finding_id": "upsell", "note": "marge à reprendre"}],
    "score": 61.5,
}

#: Charge utile produite par la MEME fonction que la persistance (`serialize_report`:
#: `indent=2`, `ensure_ascii=False`). Elle differe donc d'un `json.dumps` par defaut sur deux
#: points -- l'indentation et les accents litteraux -- et c'est exactement le piege que la
#: projection doit eviter.
PAYLOAD = serialize_report(REPORT)


class StoredDouble:
    """Ce que `persistence.analyses.StoredReport` expose, et rien de plus."""

    class _Record:
        def __init__(self, payload_sha256: str) -> None:
            self.payload_sha256 = payload_sha256

    def __init__(self, payload_json: str, *, sha256: str | None = None) -> None:
        self.payload_json = payload_json
        self.record = self._Record(sha256 if sha256 is not None else sha256_text(payload_json))


# -- le corps est les octets persistes -----------------------------------------------------

def test_the_body_is_the_persisted_text_byte_for_byte():
    assert report_body(PAYLOAD) == PAYLOAD.encode("utf-8")


def test_the_body_is_not_a_reserialisation():
    """LE PIEGE. Relire puis re-serialiser produit d'AUTRES octets: separateurs, echappement
    des accents, et un rapport sert ce que la persistance a scelle, pas ce que `json` aurait
    ecrit aujourd'hui."""
    reserialise = json.dumps(json.loads(PAYLOAD)).encode("utf-8")
    assert reserialise != PAYLOAD.encode("utf-8"), "la charge de test doit etre non canonique"
    assert report_body(PAYLOAD) != reserialise
    assert report_body(PAYLOAD) == PAYLOAD.encode("utf-8")


def test_the_body_preserves_accents_literally():
    """`serialize_report` ecrit `ensure_ascii=False`: les accents sont des octets UTF-8, pas
    des echappements `\\u00e9`. Le corps servi doit les conserver tels quels."""
    corps = report_body(PAYLOAD).decode("utf-8")
    assert "légère" in corps and "\\u00e9" not in corps


def test_the_projection_module_never_serialises_json():
    """Structurel: un `json.dumps` dans ce module casserait la fidelite octet pour octet."""
    import ast
    import pathlib

    module = pathlib.Path(__file__).resolve().parents[2] / "src" / "mervio" / "api" / "projections.py"
    arbre = ast.parse(module.read_text(encoding="utf-8"))
    appels = {noeud.attr for noeud in ast.walk(arbre) if isinstance(noeud, ast.Attribute)}
    noms = {noeud.id for noeud in ast.walk(arbre) if isinstance(noeud, ast.Name)}
    assert "dumps" not in appels | noms and "loads" not in appels | noms


# -- l'ETag ---------------------------------------------------------------------------------

def test_the_etag_is_the_persisted_fingerprint_quoted():
    empreinte = sha256_text(PAYLOAD)
    assert report_etag(PAYLOAD, payload_sha256=empreinte) == f'"{empreinte}"'


def test_the_etag_is_strong_never_weak():
    etag = report_etag(PAYLOAD, payload_sha256=sha256_text(PAYLOAD))
    assert not etag.startswith("W/")
    assert etag.startswith('"') and etag.endswith('"')


def test_the_etag_is_stable_across_calls():
    empreinte = sha256_text(PAYLOAD)
    assert report_etag(PAYLOAD, payload_sha256=empreinte) == report_etag(PAYLOAD, payload_sha256=empreinte)


def test_a_different_content_gives_a_different_etag():
    autre = PAYLOAD.replace("61.5", "61.6")
    assert autre != PAYLOAD
    assert report_etag(PAYLOAD, payload_sha256=sha256_text(PAYLOAD)) \
        != report_etag(autre, payload_sha256=sha256_text(autre))


def test_a_single_byte_changes_the_etag():
    """Meme document logique, un espace de plus: les octets different, donc l'ETag aussi."""
    espace = PAYLOAD.replace('{\n  "_meta"', '{\n   "_meta"')
    assert json.loads(espace) == json.loads(PAYLOAD)
    assert report_etag(espace, payload_sha256=sha256_text(espace)) \
        != report_etag(PAYLOAD, payload_sha256=sha256_text(PAYLOAD))


def test_a_fingerprint_that_does_not_match_the_bytes_is_refused():
    """L'empreinte persistee n'est pas crue sur parole: elle est RECALCULEE et comparee."""
    with pytest.raises(ReportIntegrityError):
        report_etag(PAYLOAD, payload_sha256=sha256_text(PAYLOAD + " "))


def test_the_integrity_refusal_reproduces_no_fingerprint():
    attendue = sha256_text(PAYLOAD + " ")
    with pytest.raises(ReportIntegrityError) as excinfo:
        report_etag(PAYLOAD, payload_sha256=attendue)
    message = str(excinfo.value)
    assert attendue not in message and sha256_text(PAYLOAD) not in message


def test_an_integrity_failure_is_an_internal_fault_not_a_client_refusal():
    """Elle n'est traduite par aucun gestionnaire de G-0: un octet modifie sous une table
    immuable est un defaut interne, donc un 500."""
    from mervio.persistence.errors import NotFound, PermissionDenied
    assert not issubclass(ReportIntegrityError, (NotFound, PermissionDenied))
    assert issubclass(ReportIntegrityError, ReportProjectionError)


# -- `If-None-Match` -------------------------------------------------------------------------

@pytest.mark.parametrize("entete, attendu", [
    ('"abc"', True),
    ('"autre"', False),
    ('*', True),
    ('"autre", "abc"', True),
    ('W/"abc"', True),
    ('W/"autre"', False),
    ('', False),
    ('"abc', False),
])
def test_if_none_match_is_compared_weakly(entete, attendu):
    """La comparaison de `If-None-Match` est FAIBLE par definition (RFC 9110 13.1.2): c'est
    `If-Match` qui exige une comparaison forte."""
    assert etag_matches('"abc"', entete) is attendu


# -- la projection complete -------------------------------------------------------------------

def test_project_report_gives_the_body_and_the_etag():
    projection = project_report(StoredDouble(PAYLOAD))
    assert isinstance(projection, ProjectedReport)
    assert projection.body == PAYLOAD.encode("utf-8")
    assert projection.etag == f'"{sha256_text(PAYLOAD)}"'
    assert projection.length == len(PAYLOAD.encode("utf-8"))


def test_project_report_refuses_a_corrupted_payload():
    with pytest.raises(ReportIntegrityError):
        project_report(StoredDouble(PAYLOAD, sha256="0" * 64))


# -- les constats, dans l'ordre ratifie --------------------------------------------------------

def test_the_findings_follow_the_four_sections_in_the_ratified_order():
    findings = derive_findings(json.loads(PAYLOAD))
    assert [f["finding_id"] for f in findings] == [
        "revenue_drop", "margin", "profitability_unavailable", "upsell"]


def test_each_finding_carries_the_category_of_its_section():
    findings = derive_findings(json.loads(PAYLOAD))
    assert [f["category"] for f in findings] == [
        "critical_issue", "warning", "setup", "opportunity"]


def test_a_finding_that_declares_its_category_keeps_it():
    rapport = {"critical_issues": [{"finding_id": "x", "category": "deja-declaree"}],
               "warnings": [], "setup": [], "opportunities": []}
    assert derive_findings(rapport)[0]["category"] == "deja-declaree"


def test_the_order_inside_a_section_is_preserved():
    rapport = {"critical_issues": [{"finding_id": "a"}, {"finding_id": "b"}, {"finding_id": "c"}],
               "warnings": [], "setup": [], "opportunities": []}
    assert [f["finding_id"] for f in derive_findings(rapport)] == ["a", "b", "c"]


def test_deriving_findings_never_modifies_the_report():
    rapport = json.loads(PAYLOAD)
    avant = json.dumps(rapport, sort_keys=True)
    derive_findings(rapport)
    assert json.dumps(rapport, sort_keys=True) == avant
    assert "category" not in rapport["critical_issues"][0]


def test_mutating_a_nested_object_of_a_finding_never_reaches_the_report():
    """LA FRONTIERE. Un constat 2.0 porte des objets IMBRIQUES -- `evidence` est une liste de
    faits structures (AN-15 / D-082), `confidence` un objet `{level, criteria}` (AN-14 / D-081),
    et `<nom>_params` un objet (AN-19 / D-085). Si la projection ne recopiait que la surface,
    un consommateur ecrivant dans l'un de ces sous-objets ecrirait DANS LE RAPPORT, alors que
    `reports` est immuable et que le rapport sert de source a d'autres lectures de la meme
    requete."""
    rapport = {
        "critical_issues": [{
            "finding_id": "revenue_drop",
            "confidence": {"level": "high", "criteria": ["baseline", "volume"]},
            "evidence": [{"fact_key": "revenue.drop", "fact_params": {"pct": -28.0}}],
        }],
        "warnings": [], "setup": [], "opportunities": [],
    }
    temoin = json.dumps(rapport, sort_keys=True)

    [constat] = derive_findings(rapport)
    constat["confidence"]["level"] = "altere"
    constat["confidence"]["criteria"].append("ajoute")
    constat["evidence"][0]["fact_params"]["pct"] = 0.0
    constat["evidence"].append({"fact_key": "ajoute"})

    assert json.dumps(rapport, sort_keys=True) == temoin, \
        "le rapport source a ete modifie a travers un objet imbrique du resultat"


def test_every_level_of_a_finding_is_detached_from_the_report():
    """Pas seulement le premier niveau imbrique: la copie descend aussi loin que la structure."""
    rapport = {"critical_issues": [{"a": {"b": {"c": ["profond"]}}}],
               "warnings": [], "setup": [], "opportunities": []}
    source = rapport["critical_issues"][0]
    [constat] = derive_findings(rapport)

    assert constat["a"] is not source["a"]
    assert constat["a"]["b"] is not source["a"]["b"]
    assert constat["a"]["b"]["c"] is not source["a"]["b"]["c"]
    constat["a"]["b"]["c"].append("ajoute")
    assert source["a"]["b"]["c"] == ["profond"]


def test_the_copy_is_json_shaped_not_a_generic_deep_copy():
    """DECISION: le rapport vient de `json.loads`, donc deux conteneurs suffisent et les
    scalaires, immuables, sont partages. Une copie profonde generique paierait les cycles, le
    protocole `__deepcopy__` et les classes inconnues pour rien."""
    import ast
    import pathlib

    module = pathlib.Path(__file__).resolve().parents[2] / "src" / "mervio" / "api" / "projections.py"
    arbre = ast.parse(module.read_text(encoding="utf-8"))
    nomme = {n.attr for n in ast.walk(arbre) if isinstance(n, ast.Attribute)}
    nomme |= {n.id for n in ast.walk(arbre) if isinstance(n, ast.Name)}
    nomme |= {a.name for n in ast.walk(arbre) if isinstance(n, (ast.Import, ast.ImportFrom))
              for a in n.names}
    assert "deepcopy" not in nomme and "copy" not in nomme

    # Et la contrepartie: un scalaire reste PARTAGE, puisqu'il est immuable.
    chaine = "une-chaine-temoin"
    rapport = {"critical_issues": [{"valeur": chaine}], "warnings": [], "setup": [],
               "opportunities": []}
    assert derive_findings(rapport)[0]["valeur"] is chaine


def test_the_section_order_matches_the_engine():
    """La duplication est IMPOSEE par la garde 1, qui interdit a `api/` d'importer le moteur.
    Un test, lui, peut importer les deux cotes -- et c'est ce qui rend la copie sure."""
    from mervio.analytics.insights import CATEGORIES, SECTION_OF
    assert tuple(section for section, _ in SECTIONS) == tuple(SECTION_OF[c] for c in CATEGORIES)
    assert tuple(category for _, category in SECTIONS) == CATEGORIES


# -- un rapport 1.x reste servi ------------------------------------------------------------------

def test_a_1x_report_without_the_setup_section_is_still_projected():
    """`setup` est la quatrieme section, ajoutee en 2.0 (AN-18 / D-084). Un rapport 1.x ne la
    porte pas et doit rester lisible (ND-04)."""
    rapport = {"critical_issues": [{"finding_id": "a"}], "warnings": [{"finding_id": "b"}],
               "opportunities": [{"finding_id": "c"}]}
    assert [f["finding_id"] for f in derive_findings(rapport)] == ["a", "b", "c"]
    assert [f["category"] for f in derive_findings(rapport)] == [
        "critical_issue", "warning", "opportunity"]


def test_only_setup_may_be_absent():
    """Les trois sections presentes depuis 1.0 sont EXIGEES: leur absence est un rapport
    casse, pas un rapport ancien."""
    assert OPTIONAL_SECTIONS == {"setup"}
    for manquante in ("critical_issues", "warnings", "opportunities"):
        rapport = {s: [] for s, _ in SECTIONS if s != manquante}
        with pytest.raises(ReportProjectionError):
            derive_findings(rapport)


def test_a_1x_report_keeps_its_own_shape_of_evidence_and_confidence():
    """1.x publiait `evidence: [str]` et `confidence: float`. La projection ne les retype pas:
    elle sert ce qui est persiste."""
    rapport = {"critical_issues": [{"finding_id": "a", "evidence": ["CA -28%"], "confidence": 0.82}],
               "warnings": [], "opportunities": []}
    [constat] = derive_findings(rapport)
    assert constat["evidence"] == ["CA -28%"] and constat["confidence"] == 0.82


# -- formes refusees -------------------------------------------------------------------------------

def test_a_null_section_is_read_as_empty():
    rapport = {"critical_issues": None, "warnings": [], "setup": None, "opportunities": []}
    assert derive_findings(rapport) == []


def test_a_section_that_is_not_a_list_is_refused():
    rapport = {"critical_issues": {"finding_id": "a"}, "warnings": [], "setup": [],
               "opportunities": []}
    with pytest.raises(ReportProjectionError):
        derive_findings(rapport)


def test_a_finding_that_is_not_an_object_is_refused():
    rapport = {"critical_issues": ["pas un constat"], "warnings": [], "setup": [],
               "opportunities": []}
    with pytest.raises(ReportProjectionError):
        derive_findings(rapport)


def test_a_malformed_report_is_an_internal_fault_not_a_client_refusal():
    from mervio.persistence.errors import NotFound, PermissionDenied
    assert not issubclass(ReportProjectionError, (NotFound, PermissionDenied))
