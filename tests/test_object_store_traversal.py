"""Confinement du pilote systeme de fichiers: attaques reelles (D-055, D-061, roadmap 004.4).

Le confinement est la PROPRIETE DE SECURITE de ce pilote. Chaque test ci-dessous echouerait
sur une implementation qui se contenterait de joindre la cle a la racine.

Rappel du modele en couches (D-061): la cle est generee cote serveur, sa forme est imposee par
une expression reguliere ET par la contrainte `raw_objects_key_format` en base; le confinement
du pilote est une defense EN PROFONDEUR, pas la frontiere primaire (la ligne sous RLS l'est).
"""
from __future__ import annotations

import io
import os
import stat
import uuid

import pytest

from mervio.storage import FilesystemObjectStore, ObjectKeyInvalid, ObjectNotFound, build_object_key
from mervio.storage.filesystem import DIRECTORY_MODE, FILE_MODE

ORG, STORE = uuid.uuid4(), uuid.uuid4()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "objects"


@pytest.fixture
def store(root):
    return FilesystemObjectStore(root)


def key() -> str:
    return build_object_key(ORG, STORE)


# -- formes de cle refusees --------------------------------------------------------------------

@pytest.mark.parametrize("attack", [
    "../../etc/passwd",
    "../" + "0" * 36,
    "org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/../../../../etc/passwd",
    "/etc/passwd",
    "/org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/" + "0" * 36,
    "org\\" + "0" * 36 + "\\store\\" + "0" * 36 + "\\raw\\" + "0" * 36,
    "org/%2e%2e/store/" + "0" * 36 + "/raw/" + "0" * 36,
    "org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/" + "0" * 36 + "\x00",
    "org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/" + "0" * 36 + "/../../x",
])
def test_a_hostile_key_never_reaches_the_filesystem(store, root, attack):
    with pytest.raises(ObjectKeyInvalid):
        store.put(attack, io.BytesIO(b"owned"))
    with pytest.raises(ObjectKeyInvalid):
        with store.open(attack):
            pass
    with pytest.raises(ObjectKeyInvalid):  # 004.4.5: la destruction passe les MEMES couches
        store.delete(attack)
    assert not root.exists() or not list(root.rglob("*")), "aucun octet ne doit avoir ete ecrit"


def test_no_hostile_key_can_write_outside_the_root(store, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("intact", encoding="utf-8")
    for attack in ("../outside.txt", "../../outside.txt", str(outside)):
        with pytest.raises(ObjectKeyInvalid):
            store.put(attack, io.BytesIO(b"owned"))
    assert outside.read_text(encoding="utf-8") == "intact"


# -- liens symboliques -------------------------------------------------------------------------

def test_a_symlinked_target_is_refused_on_read(store, root, tmp_path):
    """La cible finale est un lien vers l'exterieur: refus AVANT toute lecture."""
    secret = tmp_path / "secret.txt"
    secret.write_text("donnee-d-un-autre", encoding="utf-8")
    target = key()
    path = root.joinpath(*target.split("/"))
    path.parent.mkdir(parents=True)
    path.symlink_to(secret)

    with pytest.raises(ObjectKeyInvalid):
        with store.open(target):
            pass
    assert secret.read_text(encoding="utf-8") == "donnee-d-un-autre"


def test_a_symlinked_target_is_refused_on_write(store, root, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("intact", encoding="utf-8")
    target = key()
    path = root.joinpath(*target.split("/"))
    path.parent.mkdir(parents=True)
    path.symlink_to(victim)

    with pytest.raises(ObjectKeyInvalid):
        store.put(target, io.BytesIO(b"owned"))
    assert victim.read_text(encoding="utf-8") == "intact", "jamais d'ecriture a travers un lien"


def test_a_symlinked_parent_directory_pointing_outside_is_refused(store, root, tmp_path):
    """Le cas le plus subtil: c'est un REPERTOIRE PARENT qui est lie hors de la racine."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    target = key()
    parts = target.split("/")
    parent = root.joinpath(*parts[:-1])
    parent.parent.mkdir(parents=True)
    parent.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ObjectKeyInvalid):
        store.put(target, io.BytesIO(b"owned"))
    with pytest.raises(ObjectKeyInvalid):
        with store.open(target):
            pass
    assert not list(elsewhere.iterdir()), "rien n'a ete ecrit hors de la racine"


def test_a_symlinked_parent_pointing_inside_the_root_stays_confined(store, root):
    """Un lien qui reste SOUS la racine ne sort pas du confinement: il est tolere."""
    target = key()
    parts = target.split("/")
    real = root / "real-store"
    real.mkdir(parents=True)
    parent = root.joinpath(*parts[:-1])
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.symlink_to(real, target_is_directory=True)

    store.put(target, io.BytesIO(b"confine"))
    with store.open(target) as handle:
        assert handle.read() == b"confine"


# -- racine et permissions ---------------------------------------------------------------------

def test_the_root_must_be_absolute():
    from mervio.storage.base import ObjectStoreError
    with pytest.raises(ObjectStoreError):
        FilesystemObjectStore("relative/root")


def test_written_objects_and_directories_are_not_world_readable(store, root):
    target = key()
    store.put(target, io.BytesIO(b"payload"))
    path = root.joinpath(*target.split("/"))
    assert stat.S_IMODE(os.stat(path).st_mode) == FILE_MODE
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == DIRECTORY_MODE
    assert stat.S_IMODE(os.stat(root).st_mode) == DIRECTORY_MODE


def test_a_missing_object_under_a_valid_key_is_not_found(store):
    with pytest.raises(ObjectNotFound):
        with store.open(key()):
            pass


def test_a_directory_where_an_object_is_expected_is_not_found(store, root):
    target = key()
    path = root.joinpath(*target.split("/"))
    path.mkdir(parents=True)
    with pytest.raises(ObjectNotFound):
        with store.open(target):
            pass


# -- destruction confinee (E1, 004.4.5) --------------------------------------------------------

def test_delete_never_destroys_a_file_outside_the_root(store, tmp_path):
    """Le vecteur le plus grave de `delete`: detruire un fichier du systeme hote."""
    outside = tmp_path / "outside.txt"
    outside.write_text("intact", encoding="utf-8")
    for attack in ("../outside.txt", "../../outside.txt", str(outside)):
        with pytest.raises(ObjectKeyInvalid):
            store.delete(attack)
    assert outside.read_text(encoding="utf-8") == "intact"


def test_delete_does_not_follow_a_symlinked_target(store, root, tmp_path):
    """Couche 4: la cible liee est refusee, donc la victime pointee n'est jamais detruite."""
    victim = tmp_path / "victim.txt"
    victim.write_text("intact", encoding="utf-8")
    target = key()
    path = root.joinpath(*target.split("/"))
    path.parent.mkdir(parents=True)
    path.symlink_to(victim)

    with pytest.raises(ObjectKeyInvalid):
        store.delete(target)
    assert victim.exists() and victim.read_text(encoding="utf-8") == "intact"
    assert path.is_symlink(), "le lien lui-meme n'est pas retire non plus"


def test_delete_through_a_symlinked_parent_pointing_outside_is_refused(store, root, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    prey = elsewhere / "prey.txt"
    prey.write_text("intact", encoding="utf-8")
    target = key()
    parts = target.split("/")
    parent = root.joinpath(*parts[:-1])
    parent.parent.mkdir(parents=True)
    parent.symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ObjectKeyInvalid):
        store.delete(target)
    assert prey.read_text(encoding="utf-8") == "intact"


def test_delete_refuses_a_directory_standing_where_an_object_should_be(store, root):
    """Rendre un succes affirmerait une destruction qui n'a pas eu lieu."""
    target = key()
    path = root.joinpath(*target.split("/"))
    path.mkdir(parents=True)
    with pytest.raises(ObjectKeyInvalid):
        store.delete(target)
    assert path.is_dir()


def test_delete_removes_the_object_but_leaves_its_directories(store, root):
    """Aucune suppression recursive: seul le fichier de la cle disparait."""
    target = key()
    store.put(target, io.BytesIO(b"payload"))
    path = root.joinpath(*target.split("/"))
    store.delete(target)
    assert not path.exists()
    assert path.parent.is_dir() and root.is_dir()
