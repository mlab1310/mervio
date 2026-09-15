"""Scenarios de verite terrain contre le vrai moteur (Mission 004.0).

Chaque scenario est genere, analyse par `run_analysis` puis confronte a ses
attentes. `MATCH` doit etre retrouve; `KNOWN_GAP` documente un angle mort du
moteur actuel et doit rester non retrouve. Un echec signifie que le moteur a
change de comportement: mettre a jour le scenario avec une justification, ne
jamais affaiblir l'attente pour faire passer le test.

Volume et graines: 20 000 commandes cibles sur 18 semaines. La robustesse a
ete verifiee sur 2 profils x 5 graines (benchmarks/scenario_robustness.py).
"""
from __future__ import annotations

import logging

import pytest

from mervio.synthetic import SCENARIOS, GeneratorConfig
from mervio.synthetic.evaluation import run_scenario

PROFILES = ("fashion_eu", "electronics_us")


@pytest.fixture(autouse=True)
def _quiet_engine_logs():
    logging.disable(logging.WARNING)
    yield
    logging.disable(logging.NOTSET)


@pytest.mark.parametrize("index, name", list(enumerate(sorted(SCENARIOS))))
def test_scenario_ground_truth(tmp_path, index, name):
    config = GeneratorConfig(scenario=name, orders=20_000, days=126, seed=3, profile=PROFILES[index % 2])
    evaluation = run_scenario(config, tmp_path / name)["evaluation"]
    unsatisfied = [r for r in evaluation["results"] if not r["satisfied"]]
    assert not unsatisfied, unsatisfied
