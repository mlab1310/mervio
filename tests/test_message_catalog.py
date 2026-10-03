"""AN-19 (D-085): les TEXTES deviennent des cles de message + des parametres.

AN-15 a structure les FAITS: chaque preuve est un objet typé, et plus aucun nombre ne vit dans
une chaine de preuve. Les CONSTATS, eux, restaient de la prose francaise non accentuee, ecrite
dans le moteur et sans identite:

    fact="Le CA de 2026-W37 est inferieur de 18.7% a la moyenne des periodes precedentes."

Quatre defauts, qu'un catalogue supprime ensemble:

  * AUCUNE IDENTITE. Deux consommateurs du meme constat n'avaient que la phrase pour le
    reconnaitre. `llm/context.py` fusionnait meme les hypotheses PAR LEUR TEXTE
    (`merged.get(statement)`) et rattachait une recommandation a son constat en comparant la
    chaine rendue: une virgule changee cassait silencieusement le rattachement.
  * NON LOCALISABLE (G-27). Le texte etait la seule forme disponible.
  * NON REVISABLE. Une phrase ecrite au milieu d'un calcul n'est relue par personne.
  * NON VERIFIABLE. Rien n'empechait un enonce de deriver du modele qu'il decrit -- et c'est
    arrive: `root_causes[].method` publiait encore << Revenue = Clics x Taux de conversion x
    Panier moyen >>, le modele d'AVANT AN-01/AN-02, avec le facteur meme qu'AN-02 a supprime
    comme trompeur (D-5).

CONTRAT (D-085):

    <message>_key     identite CONTRACTUELLE, statique, stable, au catalogue
    <message>_params  les valeurs, typees, telles que le moteur les a mesurees
    <message>         le texte RENDU, derive des deux -- une representation, pas la reference

Le catalogue (`analytics/messages.py`) est statique: aucune cle construite depuis une donnee,
aucun fallback, une cle inconnue ou un parametre manquant leve. Le renderer ne calcule rien.

ASYMETRIE ASSUMEE (D-2). Les 26 cles `evidence.*` d'AN-15 restent des IDENTIFIANTS PURS, sans
texte au catalogue: le rendu d'une preuve reste DERIVE de ses champs typés (`metric_key`,
`unit`, valeurs), ce qui est tout le principe de D-082. Une entree de catalogue pour elles
reintroduirait la phrase comme source de verite.

HORS PERIMETRE (D-1), laisse a G-27 / 004.7: `kpis.*.notes`, `comparisons.*.reason`,
`profitability.*.note` et ses limitations, `limitations[]` general, messages d'ingestion,
metadonnees de rendu. Sur les 145 chaines de prose d'un rapport, AN-19 en prend les constats.
"""
from __future__ import annotations

import ast
import json
import re
import unicodedata
from datetime import date
from pathlib import Path

import pytest

from conftest import make_dataset, make_order
from mervio.analytics import messages
from mervio.analytics.messages import MESSAGES, MessageError, render, required_params
from mervio.analytics.pipeline import SourcePaths, analyze_loaded_dataset, run_analysis
from mervio.analytics.root_cause import LAYER_REVENUE
from mervio.llm import build_llm_context
from mervio.llm.context import serialize_context
from mervio.reporting.executive import render_executive_report

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"
DECLINE = Path(__file__).resolve().parent / "fixtures" / "an01_prolonged_decline"

#: modules dont les cles de message doivent etre statiques
SCANNED = ("src/mervio/analytics/insights.py", "src/mervio/analytics/anomaly.py",
           "src/mervio/analytics/root_cause.py", "src/mervio/analytics/health.py",
           "src/mervio/analytics/report.py", "src/mervio/analytics/pipeline.py",
           "src/mervio/analytics/messages.py", "src/mervio/reporting/executive.py")

#: prefixes de cles qui sont des IDENTIFIANTS, jamais des messages du catalogue (D-2)
IDENTIFIER_PREFIXES = ("evidence.", "data_freshness.")

#: orthographes NON accentuees interdites au catalogue: le francais du catalogue est accentue
#: `payants`, `revenu` et `insuffisante` n'en font PAS partie: ils s'ecrivent sans accent.
UNACCENTED = (
    "donnee", "donnees", "periode", "periodes", "precedente", "precedentes", "inferieur",
    "superieur", "detecte", "detectee", "mesuree", "mesurees", "realisee", "reelle", "reel",
    "plutot", "degage", "representent", "materialite", "expliquee", "expliquees", "paye",
    "payes", "evaluer", "observe", "observee", "observes", "elevee", "severite",
    "qualite", "deja", "ecart", "verifier", "identifie", "identifiee", "associee", "strategie",
    "a partir", "cout", "couts", "enchares", "maitrisee", "reevaluer", "reduire", "amelioration",
    "dependance", "quantites", "etablie", "causalite", "arithmetique", "demontre", "hypothese",
    "achete", "ou vient", "decision", "reference", "decomposition",
)
_UNACCENTED_RE = re.compile(r"(?<![\wÀ-ſ])(" + "|".join(UNACCENTED)
                            + r")(?![\wÀ-ſ])", re.IGNORECASE)

#: champs de constat portes par le contrat, et le suffixe de leur cle
MESSAGE_FIELDS = ("fact", "hypothesis", "recommendation", "estimated_impact_basis")

#: noms qui portent une CLE DE MESSAGE. `metric_key` n'en fait pas partie: c'est la cle d'une
#: GRANDEUR du contrat (`revenue`, `aov`), pas celle d'un texte -- elle est legitimement
#: dynamique, exactement comme les `fact_id`.
MESSAGE_KEY_NAMES = frozenset({
    "message_key", "fact_key", "hypothesis_key", "recommendation_key",
    "estimated_impact_basis_key", "criterion_key", "method_key", "observed_fact_key",
})
SECTIONS = ("critical_issues", "warnings", "setup", "opportunities")


def _paths(directory: Path) -> SourcePaths:
    kwargs = {"shopify_orders": str(directory / "shopify_orders.csv"),
              "shopify_products": str(directory / "shopify_products.csv"),
              "google_ads": str(directory / "google_ads.csv")}
    if (directory / "stripe_transactions.csv").exists():
        kwargs["stripe"] = str(directory / "stripe_transactions.csv")
    return SourcePaths(**kwargs)


@pytest.fixture(scope="module")
def report() -> dict:
    if not (SAMPLE / "shopify_orders.csv").exists():
        pytest.skip("fixtures synthetiques absentes")
    return run_analysis(_paths(SAMPLE), today=date(2026, 9, 14))


@pytest.fixture(scope="module")
def declining() -> dict:
    return run_analysis(_paths(DECLINE), today=date(2026, 9, 21))


@pytest.fixture(scope="module")
def only_setup() -> dict:
    orders = [make_order(f"#{index}", date(2026, 9, 13), f"c1:{index:02d}" + "0" * 30,
                         [("A", 1, 100.0 + index)]) for index in range(20)]
    dataset = make_dataset(orders=orders)
    dataset.quality.set_field("customer_identity", covered=len(orders), total=len(orders))
    return analyze_loaded_dataset(dataset, today=date(2026, 9, 21))


def _emitted_keys(report: dict):
    """Toute cle de message effectivement publiee par un rapport, avec son emplacement."""
    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                yield from walk(value, f"{path}.{key}" if path else key)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                yield from walk(value, f"{path}[{index}]")
        elif isinstance(node, str) and path.rsplit(".", 1)[-1] in MESSAGE_KEY_NAMES:
            yield path, node
    yield from walk(report)
    for analysis in report.get("root_causes") or []:
        for index, key in enumerate(analysis.get("limitation_keys") or []):
            yield f"root_causes[].limitation_keys[{index}]", key


# -- A. cle inconnue => echec explicite ---------------------------------------------------

def test_an_unknown_key_raises_instead_of_rendering_something():
    with pytest.raises(MessageError):
        render("finding.cette_cle_n_existe_pas", {})


def test_an_unknown_key_is_never_silently_turned_into_text():
    """Aucun repli: pas de retour de la cle, pas de chaine vide, pas de None."""
    for candidate in ("", "inconnu", "finding.", "evidence.kpi.value"):
        with pytest.raises(MessageError):
            render(candidate, {})


# -- B. parametre manquant => echec explicite ---------------------------------------------

def test_a_missing_parameter_raises():
    key = "finding.revenue_decline.fact"
    assert required_params(key), "ce message doit prendre des parametres"
    with pytest.raises(MessageError):
        render(key, {})


def test_every_catalogue_message_declares_the_parameters_it_needs():
    for key in MESSAGES:
        for name in required_params(key):
            assert name and name.isidentifier(), f"{key}: parametre invalide {name!r}"


def test_a_message_renders_exactly_when_all_its_parameters_are_given():
    key = "finding.revenue_decline.fact"
    params = {name: 0.187 if name != "period" else "2026-W37" for name in required_params(key)}
    rendered = render(key, params)
    assert rendered and "{" not in rendered


# -- C / D. catalogue statique, aucune cle dynamique --------------------------------------

def test_the_catalogue_is_a_static_mapping_of_strings():
    assert isinstance(MESSAGES, dict) and MESSAGES
    for key, template in MESSAGES.items():
        assert isinstance(key, str) and isinstance(template, str)
        assert key == key.strip() and template == template.strip()
        assert re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_]+)+", key), f"cle non conforme: {key}"


def test_the_catalogue_is_built_without_any_computation():
    """Garde statique: le catalogue est une litterale, pas un produit de code.

    Une entree calculee (boucle, f-string, concatenation depuis une donnee) ne serait ni
    relisible ni stable: c'est exactement ce que D-085 interdit.
    """
    tree = ast.parse((ROOT / "src/mervio/analytics/messages.py").read_text(encoding="utf-8"))
    catalogue = next((node for node in tree.body
                      if isinstance(node, (ast.Assign, ast.AnnAssign))
                      and any(getattr(t, "id", None) == "MESSAGES"
                              for t in ([node.target] if isinstance(node, ast.AnnAssign)
                                        else node.targets))), None)
    assert catalogue is not None, "MESSAGES doit etre une affectation de module"
    assert isinstance(catalogue.value, ast.Dict), "MESSAGES doit etre un dictionnaire litteral"
    for key, value in zip(catalogue.value.keys, catalogue.value.values):
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), ast.dump(key)
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), ast.dump(value)


def _built_from_data(node) -> bool:
    """La valeur est-elle FABRIQUEE a partir d'une donnee?

    Seule la CONSTRUCTION est interdite (D-085). Lire une cle deja construite -- une litterale,
    une entree de table statique, `self.criterion.key` -- est legitime: le site de construction
    est lui aussi scanne.
    """
    if isinstance(node, ast.JoinedStr):                      # f-string
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return True                                          # "a" + x, "%s" % x
    if isinstance(node, ast.Call):
        return getattr(node.func, "attr", None) in ("format", "join", "replace")
    if isinstance(node, ast.IfExp):
        return _built_from_data(node.body) or _built_from_data(node.orelse)
    return False


def _dynamic_message_keys(source: str, label: str):
    """Emplacements ou une cle de message serait fabriquee depuis une donnee."""
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        position = {"observed": 1, "not_observed": 1, "render": 0, "Message": 0}.get(name)
        if position is not None and len(node.args) > position \
                and _built_from_data(node.args[position]):
            offenders.append(f"{label}:{node.lineno} {name}() "
                             f"{ast.unparse(node.args[position])}")
        for keyword in node.keywords:
            if keyword.arg in MESSAGE_KEY_NAMES and _built_from_data(keyword.value):
                offenders.append(f"{label}:{node.lineno} {keyword.arg}="
                                 f"{ast.unparse(keyword.value)}")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if getattr(key, "value", None) in MESSAGE_KEY_NAMES and _built_from_data(value):
                    offenders.append(f"{label}:{node.lineno} "
                                     f"{key.value!r}: {ast.unparse(value)}")
    return offenders


def test_no_message_key_is_built_from_data():
    """Garde AST. Elle cible les CLES DE MESSAGE, pas les `fact_id`.

    `f"anomaly.{metric}"`, `f"cost.{component.key}"` et `f"factor.{primary.factor}"` sont des
    identifiants d'item de preuve, scopes par la donnee qu'ils designent: ils sont legitimes
    (AN-15) et ne doivent pas etre bloques. Seul l'argument qui porte une cle de message est
    verifie: position 1 de `observed`/`not_observed`, position 0 de `render` et de `Message`,
    plus tout mot-cle ou champ dont le nom designe une cle de message.
    """
    offenders = []
    for relative in SCANNED:
        path = ROOT / relative
        assert path.exists(), f"{relative}: module absent"
        offenders += _dynamic_message_keys(path.read_text(encoding="utf-8"), relative)
    assert not offenders, "cle de message construite dynamiquement:\n" + "\n".join(offenders)


def test_the_guard_actually_catches_a_dynamic_key():
    """Contre-epreuve: une garde qui ne detecte rien passerait sur n'importe quel depot."""
    hostile = (
        'observed(f"anomaly.{m}", f"evidence.{m}.value", metric_key=m)\n'
        'Message("finding." + kind)\n'
        'render("anomaly.%s" % kind)\n'
        'Message(key.format(kind))\n'
        'payload = {"fact_key": f"finding.{kind}.fact"}\n'
    )
    found = _dynamic_message_keys(hostile, "hostile")
    assert len(found) == 5, found


def test_the_dynamic_fact_ids_of_an15_are_still_allowed():
    """Contre-epreuve de la garde: elle ne doit pas avoir ferme la porte aux `fact_id`.

    Si elle les bloquait, AN-15 serait defait pour satisfaire AN-19: l'identite d'un item de
    preuve est SCOPEE par la donnee qu'il designe, et doit l'etre.
    """
    source = (ROOT / "src/mervio/analytics/insights.py").read_text(encoding="utf-8")
    assert 'f"factor.{primary.factor}"' in source
    assert 'f"cost.{component.key}"' in source
    assert "metric_key=component.key" in source
    assert not _dynamic_message_keys(source, "insights")


# -- E. exhaustivite des cles emises ------------------------------------------------------

def test_every_key_published_by_a_report_exists_in_the_catalogue(report, declining, only_setup):
    unknown = []
    for current in (report, declining, only_setup):
        for path, key in _emitted_keys(current):
            if key.startswith(IDENTIFIER_PREFIXES):
                continue
            if key not in MESSAGES:
                unknown.append(f"{path} = {key}")
    assert not unknown, "cles publiees sans entree au catalogue:\n" + "\n".join(unknown)


def test_every_finding_carries_a_key_for_each_text_it_publishes(report, declining):
    missing = []
    for current in (report, declining):
        for section in SECTIONS:
            for item in current[section]:
                for field in MESSAGE_FIELDS:
                    if item.get(field) and not item.get(f"{field}_key"):
                        missing.append(f"{section}[{item['type']}].{field}")
    assert not missing, "texte de constat sans cle:\n" + "\n".join(missing)


def test_the_parameters_published_cover_the_template(report, declining):
    for current in (report, declining):
        for section in SECTIONS:
            for item in current[section]:
                for field in MESSAGE_FIELDS:
                    key = item.get(f"{field}_key")
                    if not key:
                        continue
                    params = item.get(f"{field}_params") or {}
                    assert set(required_params(key)) <= set(params), f"{key}: {params}"
                    assert render(key, params) == item[field]


# -- F. accents ---------------------------------------------------------------------------

def test_the_catalogue_is_written_in_accented_french():
    accented = [m for m in MESSAGES.values() if any(ord(c) > 127 for c in m)]
    assert len(accented) >= 20, "un catalogue francais sans accents n'est pas corrige"


def test_no_catalogue_message_keeps_an_unaccented_spelling():
    offenders = []
    for key, template in MESSAGES.items():
        for match in _UNACCENTED_RE.finditer(template):
            offenders.append(f"{key}: {match.group(0)!r} dans {template!r}")
    assert not offenders, "orthographe non accentuee au catalogue:\n" + "\n".join(offenders)


def test_the_catalogue_is_normalised(report):
    """NFC: deux encodages du meme caractere accentue casseraient l'egalite d'octets."""
    for key, template in MESSAGES.items():
        assert template == unicodedata.normalize("NFC", template), key


def test_the_rendered_report_carries_the_accents(report):
    payload = json.dumps(report, ensure_ascii=False)
    assert any(ord(c) > 127 for c in payload)
    assert any(ord(c) > 127 for c in render_executive_report(report))


# -- G. determinisme ----------------------------------------------------------------------

def test_rendering_the_same_message_twice_gives_the_same_bytes():
    key = "finding.customer_concentration.fact"
    params = {name: 0.1273 for name in required_params(key)}
    assert render(key, params) == render(key, params)


def test_the_report_bytes_stay_deterministic(report):
    """Les accents ne doivent pas introduire de non-determinisme (encodage, ordre, NFC)."""
    first = json.loads(json.dumps(report))
    second = run_analysis(_paths(SAMPLE), today=date(2026, 9, 14))
    for current in (first, second):
        current["_meta"].pop("generated_at", None)
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == \
        json.dumps(second, ensure_ascii=False, sort_keys=True)


# -- H. egalite report <-> contexte LLM ---------------------------------------------------

def test_the_llm_context_carries_the_same_keys_as_the_report(report):
    context = build_llm_context(report)
    report_keys = {i["fact_key"] for section in SECTIONS for i in report[section]}
    context_keys = {f["message_key"] for f in context["findings"]}
    assert context_keys == report_keys


def test_the_llm_context_keeps_both_the_key_and_the_rendered_text(report):
    context = build_llm_context(report)
    for finding in context["findings"]:
        assert finding["message_key"] in MESSAGES
        assert finding["statement"], "le texte rendu reste present"
    for hypothesis in context["hypotheses"]:
        assert hypothesis["message_key"] in MESSAGES and hypothesis["statement"]
    for recommendation in context["recommendations"]:
        assert recommendation["message_key"] in MESSAGES and recommendation["action"]


def test_a_free_string_is_never_the_identity_of_a_message(report):
    """`llm/context.py` fusionnait les hypotheses PAR LEUR TEXTE et rattachait une
    recommandation a son constat en comparant la chaine rendue. L'identite est la cle."""
    tree = ast.parse((ROOT / "src/mervio/llm/context.py").read_text(encoding="utf-8"))
    merge = next(n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "_hypotheses")
    lookups = [ast.unparse(node) for node in ast.walk(merge)
               if isinstance(node, ast.Call)
               and ast.unparse(node.func).endswith("merged.get")]
    assert lookups and all("statement" not in call for call in lookups), lookups
    context = build_llm_context(report)
    assert all(r["source_finding"] for r in context["recommendations"]), (
        "chaque recommandation doit retrouver son constat par la cle, pas par le texte")


# -- I. evidence.* reste un identifiant pur (D-2) -----------------------------------------

def test_the_evidence_keys_have_no_catalogue_entry(report):
    """Asymetrie ASSUMEE: la preuve se rend depuis ses champs typés (AN-15 / D-082)."""
    seen = set()
    for section in SECTIONS:
        for item in report[section]:
            seen.update(fact["message_key"] for fact in item["evidence"])
    assert seen, "le rapport doit porter des preuves"
    assert all(key.startswith("evidence.") for key in seen)
    assert not (seen & set(MESSAGES)), "evidence.* ne doit PAS entrer au catalogue"


def test_the_evidence_rendering_is_still_derived_from_the_structured_fact(report):
    """Non-regression AN-15: le texte de preuve vient des champs, pas d'un gabarit."""
    text = render_executive_report(report)
    assert "PREUVE : payment_fees: " in text
    assert "non observe (source manquante: non connecte)" in text


# -- J. etat vide d'une section (D-4) -----------------------------------------------------

def test_an_empty_setup_section_uses_its_own_key(only_setup):
    assert MESSAGES["section.setup.empty"] == "Aucune donnée à connecter sur les dimensions mesurées."
    empty = dict(only_setup, setup=[])
    text = render_executive_report(empty)
    block = text[text.index("## SETUP"):text.index("## OPPORTUNITIES")]
    assert MESSAGES["section.setup.empty"] in block


def test_each_section_has_its_own_empty_state_key_without_fallback(only_setup):
    keys = {section: f"section.{section}.empty" for section in SECTIONS}
    for key in keys.values():
        assert key in MESSAGES, key
    text = render_executive_report(only_setup)
    assert MESSAGES["section.critical_issues.empty"] in text
    assert MESSAGES["section.warnings.empty"] in text
    # la phrase de SETUP ne doit pas servir aux sections de detection, ni l'inverse
    assert MESSAGES["section.setup.empty"] != MESSAGES["section.warnings.empty"]
    assert MESSAGES["section.setup.empty"] not in text[:text.index("## SETUP")]


# -- K / L. root_causes[].method (D-5) ----------------------------------------------------

EXPECTED_METHOD = ("Décomposition du revenu : Revenu = Commandes × Panier moyen. Les commandes "
                   "sont ensuite expliquées par les clics payants et les commandes par clic payé.")


def test_the_root_cause_method_states_the_two_layer_model(declining):
    analysis = declining["root_causes"][0]
    assert analysis["method_key"] == "root_cause.method"
    assert analysis["method"] == EXPECTED_METHOD
    assert MESSAGES["root_cause.method"] == EXPECTED_METHOD


def test_the_discarded_conversion_rate_factor_is_gone(declining, report):
    """D-5: le champ dont le role est d'enoncer la methode enoncait le modele d'AVANT AN-02,
    avec le facteur que AN-02 a supprime comme trompeur. Regression figee."""
    for current in (declining, report):
        for analysis in current["root_causes"]:
            assert "Taux de conversion" not in analysis["method"]
            assert "taux de conversion" not in analysis["method"].lower()
    source = (ROOT / "src/mervio/analytics/messages.py").read_text(encoding="utf-8")
    assert "Taux de conversion x Panier moyen" not in source


def test_the_method_matches_the_factors_actually_published(declining):
    """L'enonce doit decrire la decomposition REELLE: couche A = commandes x panier moyen."""
    analysis = declining["root_causes"][0]
    layer_a = {f["factor"] for f in analysis["factors"]
               if f.get("layer") == LAYER_REVENUE}
    assert layer_a == {"order_volume", "average_order_value"}
    assert "Commandes × Panier moyen" in analysis["method"]
    assert "clics payants" in analysis["method"]


# -- M. aucun fallback --------------------------------------------------------------------

def test_the_catalogue_exposes_no_default_text():
    assert not hasattr(messages, "DEFAULT_MESSAGE")
    source = (ROOT / "src/mervio/analytics/messages.py").read_text(encoding="utf-8")
    assert ".get(" not in source, "un `.get` sur le catalogue serait un fallback implicite"


def test_a_factor_label_is_resolved_from_the_catalogue_not_written_inline(declining):
    """D-3: les messages composes passent le libelle en PARAMETRE, resolu depuis la cle du
    facteur. Aucune des vingt cles facteur x variante n'a donc ete creee."""
    from mervio.analytics.messages import FACTOR_LABEL_KEY
    for factor in declining["root_causes"][0]["factors"]:
        params = factor.get("observed_fact_params") or {}
        if "factor_label" not in params:
            continue
        assert params["factor_label"] == MESSAGES[FACTOR_LABEL_KEY[factor["factor"]]]
