"""Robustesse des scenarios de verite terrain sur plusieurs profils et graines.

Les tests unitaires executent chaque scenario une fois (graine 3). Ce script
verifie que les attentes ne tiennent pas a une graine chanceuse.

Usage:
    python benchmarks/scenario_robustness.py --seeds 1,2,3,4,5 --orders 20000 --days 126
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mervio.synthetic import PROFILES, SCENARIOS, GeneratorConfig  # noqa: E402
from mervio.synthetic.evaluation import run_scenario  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", default="1,2,3,4,5")
    parser.add_argument("--orders", type=int, default=20_000)
    parser.add_argument("--days", type=int, default=126)
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)
    seeds = [int(s) for s in args.seeds.split(",")]
    summary, failures, started = {}, [], time.perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        for name in sorted(SCENARIOS):
            passed = total = 0
            for profile in sorted(PROFILES):
                for seed in seeds:
                    config = GeneratorConfig(scenario=name, orders=args.orders, days=args.days, seed=seed, profile=profile)
                    evaluation = run_scenario(config, Path(tmp) / f"{name}_{profile}_{seed}")["evaluation"]
                    total += 1
                    if evaluation["satisfied"]:
                        passed += 1
                    else:
                        failures.append({"scenario": name, "profile": profile, "seed": seed,
                                         "unsatisfied": [r for r in evaluation["results"] if not r["satisfied"]]})
            summary[name] = {"passed": passed, "runs": total}
            print(f"{name:28} {passed}/{total}", flush=True)
    result = {"recorded_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "orders": args.orders, "days": args.days, "seeds": seeds, "profiles": sorted(PROFILES),
              "duration_s": round(time.perf_counter() - started, 1), "summary": summary, "failures": failures}
    out = Path(args.out) if args.out else Path(__file__).resolve().parent / "results" / f"scenario_robustness_{date.today():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
    print(out)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
