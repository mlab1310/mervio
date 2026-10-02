"""F-01: l'ordre de `campaign_contributors` ne doit pas dependre de `PYTHONHASHSEED`.

Le defaut a DEUX causes conjointes, et corriger une seule ne suffit pas:

    for cid in set(current) | set(previous):        # (a) ordre d'iteration ~ PYTHONHASHSEED
    ...
    rows.sort(key=lambda r: r["conversions_delta"]) # (b) cle NON totale + tri STABLE

`list.sort` est stable: a cle egale, l'ordre d'entree est preserve, donc (b) PROPAGE (a).

`data/sample` ne contient AUCUNE egalite de `conversions_delta` (deltas -44, -6, -4, 1):
le test multi-processus existant (`tests/persistence/test_persistence_reports.py`) ne peut donc
pas couvrir F-01, et sa docstring le dit. Le jeu `tests/fixtures/f01_ties/` est construit pour
cela: deux egalites volontaires, sur le plus NEGATIF et sur le plus POSITIF des deltas.

    CMP-A: 10-20 = -10  |  CMP-B:  5-15 = -10   -> campaign_contributors[0] ambigu
    CMP-D: 11- 8 =  +3  |  CMP-E:  9- 6 =  +3   -> max(...) ambigu

La premiere egalite fait varier une PHRASE DESTINEE AU MARCHAND (`_recommendation_for`), la
seconde fait varier la campagne citee par `campaign_opportunity`.

Les tests de ce module n'exigent AUCUNE base de donnees: la preuve principale de F-01 doit etre
executable hors CI complete. L'identite client est fixee par une cle EXPLICITE, sans quoi la
cle ephemere de `run_analysis` (D-058, reserve 1) ferait varier les pseudonymes et masquerait
ce qu'on mesure ici.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "f01_ties"
TODAY = "2026-09-14"
#: cle d'identite FIXE: isole F-01 du non-determinisme assume des pseudonymes (D-058)
IDENTITY_HEX = "f0" * 32
SEEDS = ("0", "1", "2", "7", "42")

#: code execute dans un sous-processus: analyse le jeu avec egalites, retire le seul champ
#: non deterministe autorise (`_meta.generated_at`, D-058 reserve 1) et serialise.
_SUBPROCESS = """
import json, sys
from datetime import date
from mervio.analytics.pipeline import SourcePaths, run_analysis
from mervio.identity import CustomerIdentity
fixture, out = sys.argv[1], sys.argv[2]
report = run_analysis(
    SourcePaths(shopify_orders=fixture + "/shopify_orders.csv",
                shopify_products=fixture + "/shopify_products.csv",
                google_ads=fixture + "/google_ads.csv"),
    today=date(%d, %d, %d),
    identity=CustomerIdentity(bytes.fromhex(%r)),
)
report["_meta"].pop("generated_at", None)
with open(out, "w", encoding="utf-8") as fh:
    json.dump(report, fh, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
""" % (*(int(part) for part in TODAY.split("-")), IDENTITY_HEX)


def _report_bytes(tmp_path: Path, seed: str) -> bytes:
    """Rapport produit dans un sous-processus a `PYTHONHASHSEED=seed`, en octets."""
    out = tmp_path / f"report-{seed}.json"
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=f"{ROOT / 'src'}{os.pathsep}{ROOT / 'tests'}")
    subprocess.run([sys.executable, "-c", _SUBPROCESS, str(FIXTURE), str(out)],
                   cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=True)
    return out.read_bytes()


@pytest.fixture(scope="module")
def reports(tmp_path_factory) -> dict:
    """Un rapport par graine, chacun dans son propre processus."""
    tmp_path = tmp_path_factory.mktemp("f01")
    return {seed: json.loads(_report_bytes(tmp_path, seed)) for seed in SEEDS}


@pytest.fixture(scope="module")
def report_bytes(tmp_path_factory) -> dict:
    tmp_path = tmp_path_factory.mktemp("f01-bytes")
    return {seed: _report_bytes(tmp_path, seed) for seed in SEEDS}


def _contributors(report: dict) -> list:
    return report["root_causes"][0]["campaign_contributors"]


def _insight(report: dict, section: str, kind: str) -> dict:
    return next(i for i in report[section] if i["type"] == kind)


# -- garde du jeu de donnees -------------------------------------------------
def test_the_fixture_really_contains_tied_conversion_deltas(reports):
    """Sans egalite, tous les autres tests de ce module seraient vides de sens.

    Garde explicite: si le jeu perd ses egalites, c'est CE test qui echoue, pas les autres
    en silence.
    """
    deltas = [c["conversions_delta"] for c in _contributors(reports[SEEDS[0]])]
    assert len(deltas) != len(set(deltas)), f"aucune egalite dans {deltas}"
    assert deltas.count(min(deltas)) >= 2, "le delta le plus negatif doit etre a egalite"
    assert deltas.count(max(deltas)) >= 2, "le delta le plus positif doit etre a egalite"
    # la cause racine doit bien emprunter le chemin dangereux de `_recommendation_for`
    assert reports[SEEDS[0]]["root_causes"][0]["primary_factor"] == "conversion_rate"


# -- T-F01.1: identite d'octets ---------------------------------------------
def test_report_bytes_are_identical_across_hash_seeds(report_bytes):
    """La propriete, pas l'implementation: memes octets, quelle que soit la graine."""
    distinct = {value for value in report_bytes.values()}
    assert len(distinct) == 1, (
        f"{len(distinct)} rapports distincts sur {len(report_bytes)} graines: "
        f"{ {seed: len(value) for seed, value in report_bytes.items()} }"
    )


# -- T-F01.2: la prose metier ne varie pas ----------------------------------
def test_campaign_opportunity_names_the_same_campaign_across_hash_seeds(reports):
    """`max(..., key=conversions_delta)` est ambigu a egalite sur le plus positif."""
    facts = {seed: _insight(report, "opportunities", "campaign_opportunity")["fact"]
             for seed, report in reports.items()}
    assert len(set(facts.values())) == 1, f"la campagne citee varie: {facts}"


def test_the_recommendation_names_the_same_campaign_across_hash_seeds(reports):
    """`campaign_contributors[0]` alimente une phrase destinee au marchand.

    C'est l'occurrence la plus grave de F-01: une variable d'environnement change le texte lu
    par un humain.
    """
    recos = {seed: _insight(report, "critical_issues", "revenue_decline")["recommendation"]
             for seed, report in reports.items()}
    assert len(set(recos.values())) == 1, f"la recommandation varie: {recos}"


def test_the_whole_insight_prose_is_identical_across_hash_seeds(reports):
    """Aucune chaine de constat, de preuve ou d'hypothese ne doit varier."""
    def prose(report):
        return [
            (item["type"], item["fact"], tuple(item["evidence"]),
             item.get("hypothesis"), item.get("recommendation"))
            for section in ("critical_issues", "warnings", "setup", "opportunities")
            for item in report.get(section, [])
        ]
    rendered = {seed: prose(report) for seed, report in reports.items()}
    assert len({json.dumps(v, ensure_ascii=False) for v in rendered.values()}) == 1


# -- T-F01.3: ordre de la cause racine --------------------------------------
def test_campaign_contributor_order_is_identical_across_hash_seeds(reports):
    orders = {seed: [c["campaign_id"] for c in _contributors(report)]
              for seed, report in reports.items()}
    assert len({tuple(v) for v in orders.values()}) == 1, f"ordre instable: {orders}"


def test_campaign_contributors_are_totally_ordered(reports):
    """L'ordre doit etre celui de la cle totale `(conversions_delta, campaign_id)`.

    Asserte la PROPRIETE d'ordre, independamment de la facon dont le tri est ecrit.
    """
    rows = _contributors(reports[SEEDS[0]])
    keys = [(row["conversions_delta"], row["campaign_id"]) for row in rows]
    assert keys == sorted(keys), f"ordre non conforme a la cle totale: {keys}"


def test_product_contributors_are_totally_ordered(reports):
    """Meme propriete pour les produits: `label` n'est pas unique, `sku` l'est."""
    rows = reports[SEEDS[0]]["root_causes"][0].get("product_contributors") or []
    keys = [(-abs(row["delta"]), row["sku"]) for row in rows]
    assert keys == sorted(keys), f"ordre non conforme a la cle totale: {keys}"


# -- T-F01.4: rapport et contexte LLM s'accordent ---------------------------
def test_report_and_llm_context_agree_on_campaign_order(reports):
    """`llm/context.py` triait par `(conversions_delta, name)`: cle totale sur un champ
    DIFFERENT de celui du rapport. Consequence mesurable: une explication citant
    << la 2e campagne >> pouvait designer une autre ligne que le rapport.
    """
    from mervio.llm import build_llm_context
    report = reports[SEEDS[0]]
    context = build_llm_context(report)
    in_report = [c["name"] for c in _contributors(report)]
    in_context = [item["name"] for item in context["evidence"]
                  if item["kind"] == "campaign_contribution"]
    assert in_context == in_report[:len(in_context)], (
        f"rapport {in_report} != contexte {in_context}"
    )


def test_report_and_llm_context_agree_on_product_order(reports):
    from mervio.llm import build_llm_context
    report = reports[SEEDS[0]]
    context = build_llm_context(report)
    in_report = [p["label"] for p in report["root_causes"][0].get("product_contributors") or []]
    in_context = [item["label"] for item in context["evidence"]
                  if item["kind"] == "product_contribution"]
    assert in_context == in_report[:len(in_context)]


# -- T-F01.5: gardes statiques ----------------------------------------------
#: modules du chemin du rapport ou une cle de tri non totale produit un ordre instable
_GUARDED = (
    "src/mervio/analytics/root_cause.py",
    "src/mervio/analytics/insights.py",
    "src/mervio/llm/context.py",
    "src/mervio/synthetic/evaluation.py",
)
#: champ de tri ambigu -> champs discriminants acceptes. `key` est le nom de la colonne unique
#: passee en parametre a `root_cause._contributors` ("sku" a l'unique appel).
#:
#: `contribution` est volontairement ABSENT: `analysis.factors.sort` trie sur `abs(contribution)`
#: seul et admet des egalites (mesure sur tests/fixtures/f01_ties: `paid_traffic` et
#: `average_order_value` sont tous deux a -0.0). Son ordre est deterministe aujourd'hui, par
#: l'ordre d'ajout des facteurs, et le rendre total changerait `factors[1]` -- donc
#: `secondary_factor` cote evaluateur, donc une attente de scenario. Cela appartient a AN-02
#: (D-072, qui redefinit explicitement ce que `factors[1]` designe), pas a F-01.
_TIE_BREAKER = {
    "conversions_delta": {"campaign_id"},
    "delta": {"sku", "key"},
}


def _key_fields(expression: ast.AST) -> set:
    """Noms de champs reellement cites par une expression de cle de tri.

    Analyse l'AST plutot que le texte: `"delta" in source` serait vrai pour
    `conversions_delta`, et `"key" in source` pour n'importe quel `key=`.
    """
    fields = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            fields.add(node.value)
        elif isinstance(node, ast.Name):
            fields.add(node.id)
        elif isinstance(node, ast.Attribute):
            fields.add(node.attr)
    return fields


def _ordering_calls(tree: ast.AST):
    """Chaque appel sort/sorted/max/min portant un argument `key`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (node.func.attr if isinstance(node.func, ast.Attribute)
                else node.func.id if isinstance(node.func, ast.Name) else None)
        if name not in ("sort", "sorted", "max", "min"):
            continue
        key = next((kw.value for kw in node.keywords if kw.arg == "key"), None)
        if key is not None:
            yield node, name, key


@pytest.mark.parametrize("relative", _GUARDED)
def test_no_ordering_on_a_non_total_key_feeds_the_report(relative):
    """Toute cle de tri citant un champ ambigu doit aussi citer son champ discriminant.

    Deuxieme defense: meme si l'iteration est deja ordonnee, un `sort` ajoute plus tard sur une
    cle non totale reintroduirait F-01 sans que rien ne le signale.
    """
    tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
    offenders = []
    for node, name, key in _ordering_calls(tree):
        fields = _key_fields(key)
        for ambiguous, breakers in _TIE_BREAKER.items():
            if ambiguous in fields and not (fields & breakers):
                offenders.append(
                    f"{relative}:{node.lineno} {name}(key={ast.unparse(key)})"
                    f" -> cite {ambiguous!r} sans aucun de {sorted(breakers)}"
                )
    assert not offenders, "cles de tri non totales:\n" + "\n".join(offenders)


def test_the_report_path_never_iterates_a_set_directly():
    """`for cid in set(a) | set(b)` est la source PRIMAIRE de F-01.

    Corriger la seule cle de tri suffirait aujourd'hui, mais laisserait la source en place.
    """
    source = (ROOT / "src/mervio/analytics/root_cause.py").read_text(encoding="utf-8")
    offenders = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.For):
            continue
        rendered = ast.unparse(node.iter)
        bare_set = isinstance(node.iter, ast.Call) and getattr(node.iter.func, "id", None) == "set"
        set_union = isinstance(node.iter, ast.BinOp) and isinstance(node.iter.op, ast.BitOr) \
            and "set(" in rendered
        if (bare_set or set_union) and not rendered.startswith("sorted("):
            offenders.append(f"root_cause.py:{node.lineno} for ... in {rendered}")
    assert not offenders, "iteration directe sur un ensemble:\n" + "\n".join(offenders)
