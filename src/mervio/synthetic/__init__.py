"""Generateur de donnees SYNTHETIQUES deterministes pour Mervio (Mission 004.0).

Tout ce que ce paquet produit est fabrique. Aucune valeur ne provient d'un
marchand reel; aucun fichier genere ne doit etre presente comme une donnee
client ni entrer en production (voir `data/README.md`).

Le moteur analytique n'importe jamais ce paquet: le generateur ecrit des
fichiers aux formats que les connecteurs existants savent lire, puis le
moteur les analyse comme n'importe quel export. Les scenarios portent une
verite terrain (ce qui a ete injecte) et les attentes observables par le
moteur actuel, y compris ses angles morts declares (`KNOWN_GAP`).
"""
from __future__ import annotations

from .generator import GENERATOR_VERSION, SCHEMA_VERSION, GeneratorConfig, generate_dataset
from .profiles import PROFILES, StoreProfile
from .scenarios import SCENARIOS, Expectation, ScenarioSpec, get_scenario
from .validation import validate_dataset

__all__ = [
    "GENERATOR_VERSION", "SCHEMA_VERSION", "GeneratorConfig", "generate_dataset",
    "PROFILES", "StoreProfile", "SCENARIOS", "Expectation", "ScenarioSpec", "get_scenario",
    "validate_dataset",
]
