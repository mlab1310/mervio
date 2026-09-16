"""`requirements.lock` correspond exactement aux dependances utilisees (Mission 004.3). Sans base.

- chaque ligne est une version exacte, sans doublon;
- le lock satisfait les contraintes de `requirements.txt` et de `pyproject.toml`;
- le lock est FERME: la fermeture transitive des dependances declarees (marqueurs evalues sur
  la plateforme courante, donc Linux en CI) est exactement l'ensemble des lignes applicables;
- l'environnement qui execute les tests est celui du lock, version pour version.
"""
from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "requirements.lock"


def _lines(path: Path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            yield line


def lock_entries() -> dict:
    entries = {}
    for line in _lines(LOCK):
        requirement = Requirement(line)
        name = canonicalize_name(requirement.name)
        assert name not in entries, f"doublon dans le lock: {name}"
        entries[name] = requirement
    return entries


def pinned_version(requirement: Requirement) -> str:
    specifiers = list(requirement.specifier)
    assert len(specifiers) == 1 and specifiers[0].operator == "==", f"version non exacte: {requirement}"
    return specifiers[0].version


def applicable(requirement: Requirement, extras=("",)) -> bool:
    if requirement.marker is None:
        return True
    return any(requirement.marker.evaluate({"extra": extra}) for extra in extras)


def top_level_requirements() -> list:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    declared = list(project["dependencies"])
    for group in ("dev", "persistence"):
        declared += project["optional-dependencies"][group]
    declared += list(_lines(ROOT / "requirements.txt"))
    return [Requirement(text) for text in declared]


def test_every_lock_line_is_an_exact_version():
    entries = lock_entries()
    assert entries
    for requirement in entries.values():
        pinned_version(requirement)
        assert not requirement.extras, f"extras interdits dans le lock: {requirement}"
        assert not requirement.url, f"URL interdite dans le lock: {requirement}"


def test_the_lock_never_pins_the_installer_itself():
    assert not {"pip", "setuptools", "wheel"} & set(lock_entries())


def test_the_lock_satisfies_the_declared_constraints():
    entries = lock_entries()
    for requirement in top_level_requirements():
        name = canonicalize_name(requirement.name)
        assert name in entries, f"{requirement} absent du lock"
        assert requirement.specifier.contains(pinned_version(entries[name]), prereleases=True), requirement


def test_the_lock_is_exactly_the_transitive_closure_on_this_platform():
    entries = lock_entries()
    reached: set = set()
    pending = [(requirement, ("",)) for requirement in top_level_requirements()]
    while pending:
        requirement, parent_extras = pending.pop()
        if not applicable(requirement, parent_extras):
            continue
        name = canonicalize_name(requirement.name)
        assert name in entries, f"dependance absente du lock: {requirement}"
        pin = entries[name]
        assert applicable(pin), f"{name} requis ici mais son marqueur l'exclut du lock"
        assert requirement.specifier.contains(pinned_version(pin), prereleases=True), (
            f"{name}=={pinned_version(pin)} ne satisfait pas {requirement}")
        extras = tuple(sorted(requirement.extras)) or ("",)
        key = (name, extras)
        if key in reached:
            continue
        reached.add(key)
        for child in metadata.distribution(name).requires or ():
            pending.append((Requirement(child), extras))
    used = {name for name, _ in reached}
    expected = {name for name, requirement in entries.items() if applicable(requirement)}
    assert used == expected, f"lignes du lock inutilisees: {sorted(expected - used)}"


@pytest.mark.parametrize("name", sorted(lock_entries()))
def test_the_running_environment_is_the_locked_one(name):
    requirement = lock_entries()[name]
    if not applicable(requirement):
        with pytest.raises(metadata.PackageNotFoundError):
            metadata.version(name)
        return
    assert metadata.version(name) == pinned_version(requirement)
