"""API HTTP de Mervio (Mission 004.6). Extra OPTIONNEL `[api]`.

Le coeur analytique n'en depend pas, et ce paquet n'importe pas le moteur: voir
`docs/API_CONTRACT.md` et les gardes de `tests/api/test_api_guards.py`.
"""
from __future__ import annotations

from .contract import API_CONTRACT, API_PREFIX

__all__ = ["API_CONTRACT", "API_PREFIX"]
