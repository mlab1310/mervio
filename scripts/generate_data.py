"""Genere un jeu de donnees SYNTHETIQUE Mervio (equivalent de `mervio generate-data`).

Exemples:
    python scripts/generate_data.py --scenario healthy_store --orders 10000 --days 365 --seed 42
    python scripts/generate_data.py --scenario refund_spike --orders 20000 --days 126 --evaluate
    python scripts/generate_data.py --export-scenarios data/scenarios

Les jeux generes vont par defaut dans data/synthetic/ (ignore par Git). Rien
de ce qui est produit ne doit etre presente comme une donnee reelle.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mervio.synthetic import PROFILES, SCENARIOS, GeneratorConfig, generate_dataset, validate_dataset  # noqa: E402


def export_scenarios(directory: Path) -> list:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, spec in sorted(SCENARIOS.items()):
        path = directory / f"{name}.json"
        path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Generateur de donnees synthetiques Mervio")
    parser.add_argument("--scenario", default="healthy_store", choices=sorted(SCENARIOS))
    parser.add_argument("--profile", default="fashion_eu", choices=sorted(PROFILES))
    parser.add_argument("--orders", type=int, default=10_000)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--end-date", default="2026-09-13", help="dimanche AAAA-MM-JJ")
    parser.add_argument("--out", help="repertoire de sortie (defaut: data/synthetic/<scenario>_<profil>_<orders>_seed<seed>)")
    parser.add_argument("--validate", action="store_true", help="relit les fichiers et controle leur coherence")
    parser.add_argument("--evaluate", action="store_true", help="lance le moteur et confronte la verite terrain")
    parser.add_argument("--export-scenarios", metavar="DIR", help="ecrit les definitions de scenarios en JSON et quitte")
    args = parser.parse_args(argv)

    if args.export_scenarios:
        for path in export_scenarios(Path(args.export_scenarios)):
            print(path)
        return 0

    config = GeneratorConfig(scenario=args.scenario, profile=args.profile, orders=args.orders, days=args.days,
                             seed=args.seed, end_date=date.fromisoformat(args.end_date))
    out = Path(args.out) if args.out else ROOT / "data" / "synthetic" / \
        f"{args.scenario}_{args.profile}_{args.orders}_seed{args.seed}"
    result = generate_dataset(config, out)
    print(f"SYNTHETIC DATA -> {out}")
    print(json.dumps({"counts": result.manifest["counts"], "rows": result.counts}, indent=2))
    status = 0
    if args.validate:
        report = validate_dataset(out)
        print("validation:", report["status"])
        status |= report["status"] != "PASS"
    if args.evaluate:
        from mervio.analytics.pipeline import run_analysis
        from mervio.synthetic.evaluation import analysis_day, engine_paths, evaluate
        logging.disable(logging.WARNING)
        report = run_analysis(engine_paths(out), today=analysis_day(result.manifest))
        evaluation = evaluate(report, SCENARIOS[args.scenario], result.manifest)
        for row in evaluation["results"]:
            print(f"  [{'OK ' if row['satisfied'] else 'KO '}] {row['declared']:9} {row['check']}: "
                  f"attendu={row['expected']} observe={row['observed']}")
        status |= not evaluation["satisfied"]
    return int(status)


if __name__ == "__main__":
    raise SystemExit(main())
