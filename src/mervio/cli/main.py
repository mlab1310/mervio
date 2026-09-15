"""Interface ligne de commande.

La CLI est une INTERFACE: elle lit des arguments, appelle la couche
applicative et affiche. Elle ne contient aucune regle metier, ce qui rend le
remplacement par un handler HTTP mecanique.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional

from ..application.imports import SOURCE_LABELS, detect_source, validate_file
from ..application.service import AnalysisRequest, analyze_dataset
from ..config import ENGINE_VERSION, AnalyticsConfig
from ..errors import MervioError
from ..logging_config import configure_logging
from ..reporting.writers import write_outputs

SAMPLE_DIR = Path(__file__).resolve().parents[3] / "data" / "sample"
_STATUS_LABEL = {"valid": "VALIDE", "valid_with_issues": "VALIDE AVEC RESERVES", "invalid": "INVALIDE"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mervio.analytics",
                                     description="Mervio - analyse de sante business e-commerce")
    parser.add_argument("--version", action="version", version=f"mervio {ENGINE_VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="valide les fichiers et produit les rapports")
    _add_source_args(analyze)
    analyze.add_argument("--out", default="analysis/latest",
                         help="REPERTOIRE de sortie (report.json, report.txt, data_quality.json)")
    analyze.add_argument("--grain", choices=("week", "month"), default="week")
    analyze.add_argument("--lookback", type=int, default=12)
    analyze.add_argument("--today", help="date de reference AAAA-MM-JJ")
    analyze.add_argument("--label", default="", help="libelle libre de l'analyse")
    analyze.add_argument("--print-report", action="store_true",
                         help="affiche le rapport lisible dans le terminal")
    analyze.add_argument("--verbose", action="store_true")

    validate = sub.add_parser("validate", help="verifie des fichiers sans lancer d'analyse")
    _add_source_args(validate)
    validate.add_argument("--json", action="store_true", help="sortie JSON brute")
    validate.add_argument("--verbose", action="store_true")

    inspect = sub.add_parser(
        "inspect", help="decrit un CSV inconnu sans tenter de l'interpreter")
    inspect.add_argument("--file", action="append", default=[], dest="files", required=True,
                         help="fichier a inspecter (repetable)")
    inspect.add_argument("--out", help="repertoire ou ecrire inspection.json")
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument("--verbose", action="store_true")

    demo = sub.add_parser("demo", help="demonstration complete sur donnees SYNTHETIQUES")
    demo.add_argument("--out", default="analysis/demo")
    demo.add_argument("--today", default="2026-09-14")
    demo.add_argument("--verbose", action="store_true")
    return parser


def _add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--shopify", "--shopify-orders", dest="shopify",
                        help="export commandes Shopify (CSV)")
    parser.add_argument("--products", "--shopify-products", dest="products",
                        help="export produits Shopify avec couts (CSV)")
    parser.add_argument("--stripe", help="export transactions Stripe (CSV)")
    parser.add_argument("--google-ads", dest="google_ads", help="export campagnes Google Ads (CSV)")
    parser.add_argument("--file", action="append", default=[], dest="files",
                        help="fichier a source detectee automatiquement (repetable)")


def _no_usable_source_message(args) -> str:
    if getattr(args, "files", []):
        return (
            "erreur: aucun des fichiers fournis n'a pu etre identifie comme une source "
            "supportee (shopify_orders, shopify_products, stripe, google_ads).\n"
            "        Mervio prefere refuser un fichier plutot que deviner ses colonnes.\n"
            "        Pour decrire le fichier sans l'interpreter:\n"
            "          python -m mervio.analytics inspect --file <fichier>\n"
            "        Pour imposer une source: --shopify-orders / --shopify-products "
            "/ --stripe / --google-ads"
        )
    return "erreur: fournir au moins une source (--shopify-orders, --stripe, --google-ads ou --file)"


def _resolve_sources(args) -> dict:
    """Fusionne les options explicites et les --file auto-detectes."""
    sources = {"shopify_orders": args.shopify, "shopify_products": args.products,
               "stripe": args.stripe, "google_ads": args.google_ads}
    for path in getattr(args, "files", []):
        try:
            detected = detect_source(path)
        except MervioError as exc:
            print(f"  ! {Path(path).name}: {exc}", file=sys.stderr)
            continue
        if sources.get(detected):
            print(f"  ! {Path(path).name}: source {detected} deja fournie, fichier ignore",
                  file=sys.stderr)
            continue
        sources[detected] = path
    return sources


def _print_validations(validations, currency_note: bool = True) -> None:
    print("\nVALIDATION DES FICHIERS")
    print("-" * 78)
    for validation in validations:
        label = SOURCE_LABELS.get(validation.source, validation.source)
        print(f"  [{_STATUS_LABEL.get(validation.status, validation.status)}] {label}")
        print(f"      fichier   : {Path(validation.path).name if validation.path else '-'}")
        print(f"      lignes    : {validation.rows:,} lues | {validation.accepted_rows:,} acceptees"
              f" | {validation.rejected_rows:,} rejetees")
        if validation.period_start:
            print(f"      periode   : {validation.period_start} -> {validation.period_end}")
        if validation.currency:
            print(f"      devise    : {validation.currency}")
        if validation.error:
            print(f"      erreur    : {validation.error}")
        if validation.delimiter != ",":
            print(f"      separateur: {validation.delimiter!r}")
        for issue in validation.issues:
            suffix = f" (x{issue['count']})" if issue["count"] > 1 else ""
            print(f"      - [{issue['severity']}] {issue['message']}{suffix}")
        if validation.sensitive_findings:
            print("      /!\\ DONNEES SENSIBLES DETECTEES - a retirer avant de partager")


def _run_analysis(args, *, synthetic: bool, label: str) -> int:
    sources = _resolve_sources(args)
    if not any(sources.values()):
        print(_no_usable_source_message(args), file=sys.stderr)
        return 2

    today = datetime.strptime(args.today, "%Y-%m-%d").date() if getattr(args, "today", None) else date.today()
    request = AnalysisRequest(
        shopify_orders=sources["shopify_orders"], shopify_products=sources["shopify_products"],
        stripe=sources["stripe"], google_ads=sources["google_ads"],
        config=AnalyticsConfig(grain=getattr(args, "grain", "week"),
                               lookback_periods=getattr(args, "lookback", 12)),
        today=today, synthetic=synthetic, label=label,
    )
    result = analyze_dataset(request)
    _print_validations(result.validations)

    if not result.succeeded:
        print(f"\nANALYSE REJETEE: {result.error}", file=sys.stderr)
        return 1

    written = write_outputs(result, args.out)
    score = result.report["business_health_score"]["score"]
    print("\nANALYSE TERMINEE")
    print("-" * 78)
    print(f"  identifiant           : {result.analysis_id}")
    print(f"  Business Health Score : {score}/100" if score is not None
          else "  Business Health Score : non calculable")
    print(f"  periode               : {result.report['period']['current']['label']}")
    print("\nFICHIERS GENERES")
    print("-" * 78)
    for path in written:
        print(f"  {path}")

    if getattr(args, "print_report", False):
        print()
        print((Path(args.out) / "report.txt").read_text(encoding="utf-8"))
    return 0


def cmd_validate(args) -> int:
    sources = _resolve_sources(args)
    provided = {k: v for k, v in sources.items() if v}
    if not provided:
        # distinguer "rien fourni" de "fourni mais non reconnu": dire
        # "aucun fichier fourni" alors qu'un fichier a bien ete passe envoie
        # l'utilisateur chercher le probleme au mauvais endroit
        print(_no_usable_source_message(args), file=sys.stderr)
        return 2
    validations = [validate_file(path, source) for source, path in provided.items()]
    if args.json:
        print(json.dumps([v.to_dict() for v in validations], indent=2, ensure_ascii=False))
    else:
        _print_validations(validations)
    return 0 if all(v.usable for v in validations) else 1


def cmd_demo(args) -> int:
    missing = [n for n in ("shopify_orders.csv", "shopify_products.csv",
                           "stripe_transactions.csv", "google_ads.csv")
               if not (SAMPLE_DIR / n).exists()]
    if missing:
        print("fixtures absentes: lancer d'abord python scripts/generate_sample_data.py",
              file=sys.stderr)
        return 1

    banner = "=" * 78
    print(banner)
    print("  DEMO DATA - SYNTHETIC")
    print("  Donnees entierement fabriquees. Aucune entreprise reelle n'est concernee.")
    print(banner)
    print("\nETAPE 1/4 - IMPORT")
    for name in ("shopify_orders.csv", "shopify_products.csv",
                 "stripe_transactions.csv", "google_ads.csv"):
        print(f"  charge: data/sample/{name}")

    args.shopify = str(SAMPLE_DIR / "shopify_orders.csv")
    args.products = str(SAMPLE_DIR / "shopify_products.csv")
    args.stripe = str(SAMPLE_DIR / "stripe_transactions.csv")
    args.google_ads = str(SAMPLE_DIR / "google_ads.csv")
    args.files, args.grain, args.lookback = [], "week", 12
    args.label, args.print_report = "Demonstration (donnees synthetiques)", False

    print("\nETAPE 2/4 - VALIDATION")
    code = _run_analysis(args, synthetic=True, label=args.label)
    if code == 0:
        print("\nETAPE 3/4 - ANALYSE : terminee")
        print("ETAPE 4/4 - RAPPORTS : voir les fichiers ci-dessus")
        print()
        print(banner)
        print("  DEMO DATA - SYNTHETIC — ne jamais presenter ces chiffres a un client")
        print(banner)
    return code


def cmd_inspect(args) -> int:
    from ..application.inspector import inspect_file
    from ..application.workspace import assert_not_sample

    inspections = [inspect_file(path) for path in args.files]
    if args.json:
        print(json.dumps([i.to_dict() for i in inspections], indent=2, ensure_ascii=False))
    else:
        for inspection in inspections:
            _print_inspection(inspection)
    if args.out:
        assert_not_sample(args.out)
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "inspection.json"
        target.write_text(json.dumps([i.to_dict() for i in inspections], indent=2,
                                     ensure_ascii=False), encoding="utf-8")
        print(f"\nEcrit: {target}")
    return 0 if all(i.readable for i in inspections) else 1


def _print_inspection(inspection) -> None:
    data = inspection.to_dict()
    print("=" * 78)
    print(f"INSPECTION: {data['file']}")
    print("=" * 78)
    if not data["readable"]:
        print(f"  ILLISIBLE: {data['error']}")
        return
    print(f"  lignes          : {data['rows']:,}")
    print(f"  colonnes        : {data['columns_count']}")
    print(f"  separateur      : {data['delimiter']!r}")
    print(f"  encodage        : {data['encoding']}")
    print(f"  taille          : {data['size_bytes']:,} octets")
    print(f"  lignes en double: {data['duplicate_rows']:,}")
    period = data["period"]
    print(f"  periode         : {period['start']} -> {period['end']}"
          if period["start"] else "  periode         : aucune colonne de date reconnue")
    print(f"  devises         : {', '.join(data['currencies']) or 'aucune detectee'}")
    print(f"  granularite     : {data['granularity']['guess']} "
          f"(supposition: {data['granularity']['evidence']})")
    if data["personal_data_columns"]:
        print(f"  /!\\ colonnes potentiellement personnelles: "
              f"{', '.join(data['personal_data_columns'])}")
    if data["sensitive_findings"]:
        print("  /!\\ DONNEES SENSIBLES:")
        for finding in data["sensitive_findings"]:
            print(f"      - {finding['kind']} dans '{finding['column']}' "
                  f"({finding['occurrences']} occurrence(s))")
    print()
    print(f"  {'Colonne':<30}{'Type':<16}{'Manquant':>10}{'Distinct':>10}  PERSO")
    print("  " + "-" * 74)
    for column in data["columns"]:
        flag = "OUI" if column["looks_personal"] else ""
        print(f"  {column['column'][:29]:<30}{column['inferred_type']:<16}"
              f"{column['missing_pct']:>9.1%}{column['distinct']:>10}  {flag}")
    print()
    print("  Aucune valeur de cellule n'est affichee par cette commande.")


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(logging.DEBUG if getattr(args, "verbose", False) else logging.WARNING)
    if args.command == "analyze":
        return _run_analysis(args, synthetic=False, label=args.label)
    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "inspect":
        return cmd_inspect(args)
    if args.command == "demo":
        return cmd_demo(args)
    return 2
