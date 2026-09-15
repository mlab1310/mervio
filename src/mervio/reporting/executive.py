"""Rapport lisible par un dirigeant.

Contrainte non negociable heritee du moteur: la distinction
FACT / EVIDENCE / HYPOTHESIS / RECOMMENDATION est conservee telle quelle.
Le rendu ne reformule jamais une hypothese en fait, et n'ajoute aucun chiffre
qui ne figure pas deja dans le rapport structure.
"""
from __future__ import annotations

from typing import List, Optional

WIDTH = 78
_QUALITY_LABEL = {"reliable": "FIABLE", "incomplete": "INCOMPLET", "unavailable": "INDISPONIBLE"}
_SEVERITY_LABEL = {"high": "ELEVEE", "medium": "MOYENNE", "low": "FAIBLE", "info": "INFO"}

#: ordre d'affichage des indicateurs, partage par KEY KPIs et la tracabilite
_KPI_ORDER = ("revenue", "orders", "aov", "units", "ad_spend", "roas", "cac",
              "new_customers", "paid_conversion_rate", "gross_margin", "refunds", "refund_rate")

SYNTHETIC_BANNER = [
    "!" * WIDTH,
    "!!  DEMO DATA - SYNTHETIC".ljust(WIDTH - 2) + "!!",
    "!!  Ces chiffres proviennent de donnees FABRIQUEES.".ljust(WIDTH - 2) + "!!",
    "!!  Ils ne decrivent aucune entreprise reelle.".ljust(WIDTH - 2) + "!!",
    "!" * WIDTH,
    "",
]


def _title(text: str) -> List[str]:
    return ["", f"## {text}", "-" * WIDTH]


def _money(value: Optional[float], currency: str) -> str:
    return "indisponible" if value is None else f"{value:,.2f} {currency}"


def _pct(value: Optional[float]) -> str:
    return "indisponible" if value is None else f"{value:.1%}"


def _wrap(text: str, indent: int = 9, width: int = WIDTH) -> List[str]:
    import textwrap
    return textwrap.wrap(text, width=width - indent) or [""]


def _insight_block(item: dict, currency: str) -> List[str]:
    pad = " " * 9
    lines = [f"  [{_SEVERITY_LABEL.get(item['severity'], item['severity'].upper())}]"]
    for index, chunk in enumerate(_wrap(item["fact"])):
        lines.append(("  FACT   : " if index == 0 else pad + "  ") + chunk)
    for evidence in item["evidence"]:
        for index, chunk in enumerate(_wrap(evidence, indent=12)):
            lines.append(("  PREUVE : " if index == 0 else pad + "  ") + chunk)
    if item.get("hypothesis"):
        for index, chunk in enumerate(_wrap(item["hypothesis"])):
            lines.append(("  HYPOTH.: " if index == 0 else pad + "  ") + chunk)
    if item.get("recommendation"):
        for index, chunk in enumerate(_wrap(item["recommendation"])):
            lines.append(("  ACTION : " if index == 0 else pad + "  ") + chunk)
    impact = item.get("estimated_impact")
    lines.append("  IMPACT : " + (
        f"{impact:,.2f} {currency} — {item.get('estimated_impact_basis') or ''}".strip(" —")
        if impact is not None
        else "non chiffrable a partir des donnees disponibles"
    ))
    lines.append(f"  CONF.  : {item['confidence']:.2f} (indice de confiance, pas une probabilite)")
    lines.append("")
    return lines


def render_executive_report(report: dict) -> str:
    meta = report["_meta"]
    currency = meta["currency"]
    health = report["business_health_score"]
    current = report["period"]["current"]
    lines: List[str] = []

    if meta.get("dataset_is_synthetic"):
        lines.extend(SYNTHETIC_BANNER)

    lines += [
        "=" * WIDTH,
        "MERVIO BUSINESS HEALTH REPORT",
        "=" * WIDTH,
        f"Business Health Score : {health['score']}/100" if health["score"] is not None
        else "Business Health Score : non calculable",
        f"Periode               : {current['label']} ({current['start']} au {current['end']})",
        f"Reference             : {report['period']['previous']['label']}",
        f"Devise                : {currency}",
        f"Genere le             : {meta['generated_at']}",
        f"Moteur                : {meta['engine']} v{meta['engine_version']} "
        f"(LLM utilise: {'oui' if meta['llm_used'] else 'non'})",
    ]
    if health["score"] is not None:
        lines.append("")
        for chunk in _wrap(health["interpretation"], indent=2):
            lines.append("  " + chunk)

    lines += _title("EXECUTIVE SUMMARY")
    for chunk in _wrap(report["executive_summary"], indent=2):
        lines.append("  " + chunk)
    if health["excluded_dimensions"]:
        lines.append("")
        lines.append("  Dimensions non notees faute de donnees: "
                     + ", ".join(health["excluded_dimensions"]) + ".")

    for section, key in (("CRITICAL ISSUES", "critical_issues"),
                         ("WARNINGS", "warnings"),
                         ("OPPORTUNITIES", "opportunities")):
        lines += _title(section)
        items = report[key]
        if not items:
            lines.append("  Aucun element detecte sur les dimensions mesurees.")
            continue
        for item in items:
            lines += _insight_block(item, currency)

    lines += _title("KEY KPIs")
    for key in _KPI_ORDER:
        metric = report["kpis"].get(key)
        if not metric:
            continue
        if metric["value"] is None:
            rendered = "indisponible"
        elif metric["unit"] == "currency":
            rendered = _money(metric["value"], currency)
        elif key in ("gross_margin", "refund_rate", "paid_conversion_rate"):
            rendered = _pct(metric["value"])
        elif metric["unit"] == "ratio":
            rendered = f"{metric['value']:.2f}"
        else:
            rendered = f"{metric['value']:,.0f}"
        lines.append(f"  {metric['label']:<32} {rendered:>22}   [{_QUALITY_LABEL[metric['data_quality']]}]")
        for note in metric["notes"]:
            for chunk in _wrap(note, indent=8):
                lines.append("      . " + chunk)

    lines += _title("TRACABILITE DES CHIFFRES")
    lines.append("  Chaque indicateur ci-dessus est calcule par cette formule, a partir de")
    lines.append("  ces fichiers sources. Aucun chiffre n'est estime.")
    lines.append("")
    for key in _KPI_ORDER:
        metric = report["kpis"].get(key)
        if not metric:
            continue
        lines.append(f"  {metric['label']}")
        lines.append(f"      formule : {metric['formula']}")
        lines.append(f"      sources : {', '.join(metric['sources'])}")

    series = report.get("time_series", {}).get("revenue", {})
    comparisons = report.get("comparisons", {}).get("revenue", {})
    lines += _title("REVENUE ANALYSIS")
    revenue = report["kpis"].get("revenue")
    if revenue:
        lines.append(f"  CA avant ajustements           {_money(revenue['value'], currency):>22}")
        lines.append(f"      definition: {revenue['definition']}")
        lines.append("      ne reproduit pas les ventes nettes (net sales) Shopify, qui peuvent differer")
    for kind, label in (("wow", "Semaine vs semaine precedente"),
                        ("mom", "Mois vs mois precedent"),
                        ("yoy", "Annee vs annee precedente")):
        comparison = comparisons.get(kind)
        if not comparison:
            continue
        if not comparison["available"]:
            lines.append(f"  {label:<30} indisponible")
            for chunk in _wrap(comparison["reason"], indent=8):
                lines.append("      . " + chunk)
            continue
        lines.append(f"  {label:<30} {comparison['pct_change']:>+21.1%}")
        lines.append(f"      {comparison['current_period']}: "
                     f"{_money(comparison['current_value'], currency)}  |  "
                     f"{comparison['previous_period']}: "
                     f"{_money(comparison['previous_value'], currency)}")
    points = series.get("points", [])
    if points:
        lines.append("")
        lines.append(f"  Historique du CA ({len(points)} periodes, moyenne mobile sur "
                     f"{series.get('rolling_window')} periodes)")
        averages = series.get("rolling_average", [])
        lines.append(f"  {'Periode':<14}{'CA':>16}{'Moy. mobile':>16}")
        for index, point in enumerate(points):
            value = "indisponible" if point["value"] is None else f"{point['value']:,.2f}"
            average = averages[index] if index < len(averages) else None
            rendered = "-" if average is None else f"{average:,.2f}"
            lines.append(f"  {point['period']:<14}{value:>16}{rendered:>16}")

    profit = report["profitability"]
    lines += _title("PROFITABILITY")
    lines.append(f"  CA avant ajustements           {_money(profit['revenue'], currency):>22}")
    if profit["data_available"]:
        lines.append(f"  Profit de contribution         {_money(profit['contribution_profit'], currency):>22}")
        lines.append(f"  Marge de contribution          {_pct(profit['contribution_margin']):>22}")
    else:
        lines.append("  Profit de contribution complet : NON CALCULABLE")
        if profit["partial_contribution_profit"] is not None:
            lines.append(f"  Profit PARTIEL                 {_money(profit['partial_contribution_profit'], currency):>22}")
            lines.append(f"  Marge PARTIELLE                {_pct(profit['partial_contribution_margin']):>22}")
            lines.append("  Couts inclus  : " + ", ".join(profit["included_components"]))
        lines.append("  Couts MANQUANTS: " + ", ".join(profit["missing_components"]))
        lines.append("")
        lines.append("  Un profit partiel n'est pas un profit. Tant que ces couts ne sont pas")
        lines.append("  fournis, ce rapport mesure du chiffre d'affaires, pas de la rentabilite.")

    customers = report["customers"]
    lines += _title("CUSTOMER HEALTH")
    lines.append(f"  Clients actifs sur la periode  {customers['total_active']:>22,}")
    for key, label in (("new_customers", "Nouveaux clients"),
                       ("repeat_purchase_rate", "Taux de reachat"),
                       ("concentration_top5", "Concentration top 5")):
        metric = customers.get(key)
        if not metric:
            continue
        value = metric["value"]
        rendered = ("indisponible" if value is None
                    else (f"{value:.1%}" if metric["unit"] == "ratio" else f"{value:,.0f}"))
        lines.append(f"  {label:<30} {rendered:>22}")

    lines += _title("PRODUCT HEALTH")
    products = report["products"][:8]
    if not products:
        lines.append("  Aucun article vendu sur la periode.")
    else:
        lines.append(f"  {'Produit':<32}{'CA':>14}{'Unites':>9}{'Marge brute':>14}")
        for product in products:
            margin = "inconnue" if product["gross_margin"] is None else f"{product['gross_margin']:.1%}"
            title = product["title"][:31]
            lines.append(f"  {title:<32}{product['revenue']:>14,.2f}{product['units']:>9}{margin:>14}")
        if any(p["gross_margin"] is None for p in products):
            lines.append("")
            lines.append("  'inconnue' signifie que le cout d'achat n'a pas ete fourni,")
            lines.append("  pas que la marge est nulle.")

    marketing = report["marketing"]
    lines += _title("MARKETING")
    for key, label in (("ad_spend", "Depense publicitaire"), ("roas", "ROAS"), ("cac", "CAC")):
        metric = marketing.get(key)
        if not metric:
            continue
        value = metric["value"]
        rendered = ("indisponible" if value is None
                    else (f"{value:.2f}" if metric["unit"] == "ratio" else _money(value, currency)))
        lines.append(f"  {label:<30} {rendered:>22}")
    if marketing["campaigns"]:
        lines.append("")
        lines.append(f"  {'Campagne':<28}{'Depense':>12}{'Clics':>9}{'Conv.':>9}{'CPA':>11}")
        for campaign in marketing["campaigns"][:8]:
            cpa = "n/a" if campaign["cpa"] is None else f"{campaign['cpa']:,.2f}"
            lines.append(f"  {campaign['name'][:27]:<28}{campaign['spend']:>12,.2f}"
                         f"{campaign['clicks']:>9,}{campaign['conversions']:>9,.0f}{cpa:>11}")

    lines += _title("ROOT CAUSES")
    if not report["root_causes"]:
        lines.append("  Aucune analyse de cause disponible.")
    for analysis in report["root_causes"]:
        if not analysis["available"]:
            lines.append("  Analyse indisponible:")
            for limitation in analysis["limitations"]:
                for chunk in _wrap(limitation, indent=6):
                    lines.append("    - " + chunk)
            continue
        lines.append(f"  Cible   : {analysis['target_metric']} {analysis['observed_change_pct']:+.1%} "
                     f"({analysis['previous_period']} -> {analysis['period']})")
        for chunk in _wrap("Methode : " + analysis["method"], indent=4):
            lines.append("  " + chunk)
        lines.append("")
        for factor in analysis["factors"]:
            share = "   n/a" if factor["contribution"] is None else f"{factor['contribution']:>6.0%}"
            lines.append(f"    {share}  {factor['observed_fact']}")
            for chunk in _wrap("hypothese: " + factor["hypothesis"], indent=14):
                lines.append("            " + chunk)
        lines.append("")
        lines.append("  Ces contributions decrivent une decomposition arithmetique.")
        lines.append("  Elles n'etablissent pas un lien de causalite prouve.")

    lines += _title("RECOMMENDATIONS")
    if not report["recommendations"]:
        lines.append("  Aucune recommandation generee.")
    for index, reco in enumerate(report["recommendations"], start=1):
        for position, chunk in enumerate(_wrap(reco["recommendation"], indent=6)):
            lines.append((f"  {index}. " if position == 0 else "     ") + chunk)
        impact = reco.get("estimated_impact")
        lines.append(f"     (origine: {reco['source_insight']}, severite: "
                     f"{_SEVERITY_LABEL.get(reco['severity'], reco['severity'])}, "
                     f"confiance: {reco['confidence']:.2f}, impact: "
                     + (f"{impact:,.2f} {currency})" if impact is not None else "non chiffrable)"))

    quality = report["data_quality"]
    lines += _title("DATA QUALITY")
    lines.append("  Sources chargees: " + (", ".join(quality["sources_loaded"]) or "aucune"))
    if quality.get("row_counts"):
        lines.append("")
        lines.append(f"  {'Source':<24}{'Lignes':>10}{'Acceptees':>12}{'Rejetees':>10}")
        for count in quality["row_counts"].values():
            lines.append(f"  {count['source']:<24}{count['rows']:>10,}"
                         f"{count['accepted_rows']:>12,}{count['rejected_rows']:>10,}")
    lines.append("")
    for field_quality in quality["fields"]:
        lines.append(f"  [{_QUALITY_LABEL[field_quality['status']]:<12}] {field_quality['field']:<22}"
                     f" couverture {field_quality['coverage']:.0%}")
        if field_quality["note"]:
            for chunk in _wrap(field_quality["note"], indent=8):
                lines.append("      . " + chunk)
    if quality["issues"]:
        lines.append("")
        lines.append("  Incidents releves:")
        for issue in quality["issues"]:
            suffix = f" (x{issue['count']})" if issue["count"] > 1 else ""
            for position, chunk in enumerate(_wrap(f"[{issue['severity']}] {issue['message']}{suffix}", indent=6)):
                lines.append(("    - " if position == 0 else "      ") + chunk)

    lines += _title("LIMITATIONS")
    for limitation in report["limitations"]:
        for position, chunk in enumerate(_wrap(limitation, indent=6)):
            lines.append(("  - " if position == 0 else "    ") + chunk)

    lines += ["", "=" * WIDTH,
              "Tous les chiffres de ce rapport sont calcules de maniere deterministe.",
              "Aucun modele de langage n'intervient dans leur production.",
              "=" * WIDTH]
    if meta.get("dataset_is_synthetic"):
        lines += SYNTHETIC_BANNER
    return "\n".join(lines)
