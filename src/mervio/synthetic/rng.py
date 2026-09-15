"""Aleas deterministes par espace de noms.

Chaque etape (un jour, le catalogue, ...) recoit sa propre graine derivee de
la graine racine: ajouter un jour ou changer le volume ne decale pas les
tirages des autres etapes, et deux executions identiques produisent les memes
octets. Idee etudiee dans ablancogcr/synthetic-dataset-generator (MIT),
reimplementee ici avec la seule bibliotheque standard.
"""
from __future__ import annotations

import hashlib
import random


def stable_seed(root_seed: int, namespace: str) -> int:
    digest = hashlib.sha256(f"{root_seed}:{namespace}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little")


def rng_for(root_seed: int, namespace: str) -> random.Random:
    return random.Random(stable_seed(root_seed, namespace))
