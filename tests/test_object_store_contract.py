"""Suite de CONTRAT du magasin d'objets, executee contre les TROIS pilotes (D-055).

D-055 exige une seule suite, passee sur memoire, systeme de fichiers et S3. Le pilote S3 est
exerce avec `moto` EN PROCESSUS: aucun compte AWS, aucun reseau, aucun endpoint externe.

Ce fichier fixe aussi, explicitement, ce que le contrat NE promet PAS: le non-ecrasement
(D-061). La propriete est absente des trois pilotes, volontairement et identiquement.
"""
from __future__ import annotations

import hashlib
import io
import uuid

import pytest

from mervio.storage import (
    CHUNK_SIZE, FilesystemObjectStore, MemoryObjectStore, ObjectKeyInvalid, ObjectNotFound,
    ObjectStore, build_object_key,
)

ORG, STORE = uuid.uuid4(), uuid.uuid4()


def key() -> str:
    return build_object_key(ORG, STORE)


class CountingSource(io.BytesIO):
    """Source qui retient la plus grande lecture demandee: prouve une consommation bornee."""

    def __init__(self, payload: bytes) -> None:
        super().__init__(payload)
        self.largest_read = 0
        self.seeks = 0

    def read(self, size=-1):  # noqa: D102
        self.largest_read = max(self.largest_read, size if size is not None and size >= 0 else 1 << 62)
        return super().read(size)

    def seek(self, *args, **kwargs):  # noqa: D102
        self.seeks += 1
        return super().seek(*args, **kwargs)


# -- pilotes ----------------------------------------------------------------------------------

@pytest.fixture
def memory_store():
    return MemoryObjectStore()


@pytest.fixture
def filesystem_store(tmp_path):
    return FilesystemObjectStore(tmp_path / "objects")


@pytest.fixture
def s3_store():
    """Bucket moto en processus. Identifiants factices: rien ne sort du processus."""
    import boto3
    from moto import mock_aws

    from mervio.storage.s3 import S3ObjectStore

    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1",
                              aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket="mervio-objects")
        yield S3ObjectStore("mervio-objects", client=client)


@pytest.fixture(params=["memory", "filesystem", "s3"])
def store(request):
    return request.getfixturevalue(f"{request.param}_store")


# -- contrat ----------------------------------------------------------------------------------

def test_every_driver_satisfies_the_object_store_protocol(store):
    assert isinstance(store, ObjectStore)


def test_bytes_written_are_the_bytes_read_back(store):
    target, payload = key(), b"order_id,total\nA-1,10.00\n"
    store.put(target, io.BytesIO(payload))
    with store.open(target) as handle:
        assert handle.read() == payload


def test_put_reports_the_sha256_and_size_of_what_it_wrote(store):
    target, payload = key(), b"col\n" + b"row\n" * 1000
    result = store.put(target, io.BytesIO(payload))
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.byte_size == len(payload)


def test_an_empty_object_round_trips(store):
    target = key()
    result = store.put(target, io.BytesIO(b""))
    assert result.byte_size == 0
    assert result.sha256 == hashlib.sha256(b"").hexdigest()
    with store.open(target) as handle:
        assert handle.read() == b""


def test_an_object_larger_than_one_chunk_round_trips_with_a_correct_digest(store):
    target = key()
    payload = (b"abcdefgh" * 64) * (CHUNK_SIZE // 512 + 7)  # > CHUNK_SIZE
    assert len(payload) > CHUNK_SIZE
    result = store.put(target, io.BytesIO(payload))
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.byte_size == len(payload)
    with store.open(target) as handle:
        assert handle.read() == payload


def test_put_consumes_the_source_in_bounded_reads(store):
    """Memoire bornee: aucune lecture ne demande plus d'un bloc, quelle que soit la taille."""
    payload = b"x" * (CHUNK_SIZE * 3 + 17)
    source = CountingSource(payload)
    store.put(key(), source)
    assert 0 < source.largest_read <= CHUNK_SIZE, source.largest_read


def test_put_does_not_close_the_source_it_was_given(store):
    source = io.BytesIO(b"payload")
    store.put(key(), source)
    assert not source.closed


def test_opening_a_key_that_was_never_written_raises_object_not_found(store):
    with pytest.raises(ObjectNotFound):
        with store.open(key()):
            pass


def test_two_distinct_keys_hold_distinct_objects(store):
    first, second = key(), key()
    store.put(first, io.BytesIO(b"first"))
    store.put(second, io.BytesIO(b"second"))
    with store.open(first) as handle:
        assert handle.read() == b"first"
    with store.open(second) as handle:
        assert handle.read() == b"second"


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "/etc/passwd", "org/../../etc/passwd", "org/x/store/y/raw/z",
    "", "raw/" + "0" * 36, "org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/" + "0" * 35,
    "org\\x\\store\\y\\raw\\z", "org/%2e%2e/store/x/raw/y",
])
def test_a_key_outside_the_canonical_form_is_refused_by_put(store, bad):
    with pytest.raises(ObjectKeyInvalid):
        store.put(bad, io.BytesIO(b"x"))


def test_a_key_outside_the_canonical_form_is_refused_by_open(store):
    with pytest.raises(ObjectKeyInvalid):
        with store.open("../../etc/passwd"):
            pass


def test_a_key_carrying_a_nul_byte_is_refused(store):
    with pytest.raises(ObjectKeyInvalid):
        store.put(key().replace("raw/", "raw\x00/"), io.BytesIO(b"x"))


def test_a_non_string_key_is_refused(store):
    with pytest.raises(ObjectKeyInvalid):
        store.put(None, io.BytesIO(b"x"))


def test_open_closes_the_handle_when_the_block_ends(store):
    target = key()
    store.put(target, io.BytesIO(b"payload"))
    with store.open(target) as handle:
        pass
    assert handle.closed


# -- ce que le contrat NE promet PAS (D-061) ---------------------------------------------------

def test_the_contract_offers_no_overwrite_protection_on_any_driver(store):
    """D-061: la garantie de non-ecrasement est ABSENTE des trois pilotes, identiquement.

    Sur S3 elle exigerait une ecriture conditionnelle, reportee a 004.9 par D-055; un
    `head_object` suivi d'un `put_object` serait sujet a une course. L'unicite est portee par
    les cles `uuid4` et par `UNIQUE (organization_id, object_key)` en base, jamais par le magasin.
    """
    target = key()
    store.put(target, io.BytesIO(b"first"))
    store.put(target, io.BytesIO(b"second"))
    with store.open(target) as handle:
        assert handle.read() == b"second"


def test_the_contract_exposes_no_object_already_exists_error():
    import mervio.storage as storage
    assert not hasattr(storage, "ObjectAlreadyExists")


def test_the_contract_still_exposes_no_stat(store):
    """`delete` est arrivee en 004.4.5 (D-056); `stat` reste hors perimetre (ramassage, 004.9)."""
    assert not hasattr(store, "stat")


# -- destruction (E1, 004.4.5, D-056) ----------------------------------------------------------

def test_delete_removes_the_bytes_of_an_existing_object(store):
    target = key()
    store.put(target, io.BytesIO(b"identity-bearing"))
    store.delete(target)
    with pytest.raises(ObjectNotFound):
        with store.open(target):
            pass


def test_delete_on_a_key_that_was_never_written_is_not_an_error(store):
    """Idempotence, cas 1: un travail repris ne doit pas echouer sur ce qu'il n'a jamais ecrit."""
    store.delete(key())


def test_delete_twice_is_idempotent_and_restores_nothing(store):
    """Idempotence, cas 2: rejouer un effacement ne leve pas et ne fait pas revivre les octets."""
    target = key()
    store.put(target, io.BytesIO(b"identity-bearing"))
    store.delete(target)
    store.delete(target)
    with pytest.raises(ObjectNotFound):
        with store.open(target):
            pass


def test_delete_touches_no_other_key(store):
    """La seule unite de destruction est LA cle: aucun joker, aucun prefixe, aucun balayage."""
    doomed, kept = key(), key()
    store.put(doomed, io.BytesIO(b"doomed"))
    store.put(kept, io.BytesIO(b"kept"))
    store.delete(doomed)
    with store.open(kept) as handle:
        assert handle.read() == b"kept"


def test_delete_leaves_the_neighbours_of_another_store_and_organization_intact(store):
    """Confinement de tenant: deux cles d'organisations differentes ne se detruisent pas l'une
    l'autre, alors meme qu'un pilote fichier les range sous des repertoires voisins."""
    other_org, other_store = uuid.uuid4(), uuid.uuid4()
    mine, theirs = key(), build_object_key(other_org, other_store)
    store.put(mine, io.BytesIO(b"mine"))
    store.put(theirs, io.BytesIO(b"theirs"))
    store.delete(mine)
    with store.open(theirs) as handle:
        assert handle.read() == b"theirs"


@pytest.mark.parametrize("bad", [
    "../../etc/passwd", "/etc/passwd", "org/../../etc/passwd", "org/x/store/y/raw/z",
    "", "raw/" + "0" * 36, "org\\x\\store\\y\\raw\\z", "org/%2e%2e/store/x/raw/y",
    "org/*/store/*/raw/*", "org/" + "0" * 36 + "/store/" + "0" * 36 + "/raw/",
])
def test_a_key_outside_the_canonical_form_is_refused_by_delete(store, bad):
    """Aucune evasion et AUCUN joker: une forme non canonique ne detruit rien, jamais."""
    with pytest.raises(ObjectKeyInvalid):
        store.delete(bad)


def test_delete_refuses_a_non_string_key(store):
    with pytest.raises(ObjectKeyInvalid):
        store.delete(None)


def test_delete_refuses_a_key_carrying_a_nul_byte(store):
    with pytest.raises(ObjectKeyInvalid):
        store.delete(key().replace("raw/", "raw\x00/"))


def test_a_refused_key_destroys_nothing_at_all(store):
    """Preuve que le refus precede l'operation: rien n'a disparu apres une cle rejetee."""
    target = key()
    store.put(target, io.BytesIO(b"kept"))
    for bad in ("../../etc/passwd", "", "org/*/store/*/raw/*", None):
        with pytest.raises(ObjectKeyInvalid):
            store.delete(bad)
    with store.open(target) as handle:
        assert handle.read() == b"kept"


def test_a_key_can_be_written_again_after_being_deleted(store):
    """La destruction ne pose aucun tombstone dans le magasin: la cle redevient ecrivable.

    Le magasin n'est pas une frontiere de securite (D-054): c'est la ligne `raw_objects` sous
    RLS qui interdit la reutilisation d'une cle, jamais le pilote.
    """
    target = key()
    store.put(target, io.BytesIO(b"first"))
    store.delete(target)
    store.put(target, io.BytesIO(b"second"))
    with store.open(target) as handle:
        assert handle.read() == b"second"


def test_delete_is_part_of_the_protocol_on_every_driver(store):
    """Parite: les trois pilotes exposent la MEME methode, appelable de la meme facon (D-055)."""
    assert callable(getattr(store, "delete", None))


# =============================================================================================
# Contrat d'erreur TOTAL et capacite de destruction (004.4.5, D-064)
# =============================================================================================
#
# D-064 etend le contrat: aucune exception NATIVE de pilote ne franchit l'abstraction. Ces tests
# provoquent de VRAIS echecs de pilote -- systeme de fichiers non inscriptible, fournisseur qui
# refuse -- et verifient que ce qui sort est un `ObjectStoreError`, sans valeur dans le message.
#
# Pourquoi cela compte: le gestionnaire d'effacement client classe `ObjectStoreError` en
# `object_delete_failed` REPRENABLE. Une exception native contournait cette branche et publiait un
# code derive du nom de classe Python (`permission_error`), en laissant la ligne `purging`.
#
# Ces tests ne passent PAS par la fixture `store` parametree: ils sont specifiques au pilote, parce
# qu'un echec natif se provoque differemment sur un systeme de fichiers et chez un fournisseur.

import errno as _errno
import os as _os

from mervio.storage import (
    DELETE_CAPABILITIES, DELETE_CAPABLE, DELETE_INCAPABLE, DELETE_UNDETERMINED, ObjectStoreError,
)

#: Fragments qui ne doivent JAMAIS apparaitre dans un message d'erreur de magasin.
FORBIDDEN_IN_MESSAGE = ("/", "\\", "mervio-objects", "mervio_objects", "amazonaws", "http")


def _unwritable(path):
    """Rend l'arbre non inscriptible, comme un montage `:ro` du point de vue de `unlink`."""
    for entry in sorted(path.rglob("*"), reverse=True):
        if entry.is_dir():
            _os.chmod(entry, 0o500)
    _os.chmod(path, 0o500)


def _restore(path):
    _os.chmod(path, 0o700)
    for entry in path.rglob("*"):
        if entry.is_dir():
            _os.chmod(entry, 0o700)


def _assert_safe(exc: BaseException) -> str:
    """Un echec de magasin: jamais une exception native, jamais une valeur dans le message."""
    assert isinstance(exc, ObjectStoreError), type(exc)
    assert not isinstance(exc, OSError), "une exception native a franchi l'abstraction"
    message = str(exc)
    for fragment in FORBIDDEN_IN_MESSAGE:
        assert fragment not in message, (fragment, message)
    return message


# -- pilote systeme de fichiers ----------------------------------------------------------------

def test_the_filesystem_driver_never_lets_a_native_oserror_escape_from_delete(tmp_path):
    """LE cas de D-064: racine non inscriptible, donc `unlink` refuse par le systeme."""
    root = tmp_path / "objects"
    store = FilesystemObjectStore(root)
    target = key()
    store.put(target, io.BytesIO(b"payload"))
    _unwritable(root)
    try:
        with pytest.raises(ObjectStoreError) as refused:
            store.delete(target)
    finally:
        _restore(root)
    message = _assert_safe(refused.value)
    # la CLASSE d'erreur est conservee, elle seule: un operateur doit pouvoir diagnostiquer
    assert _errno.errorcode[_errno.EACCES] in message or _errno.errorcode[_errno.EROFS] in message
    assert "destruction" in message


def test_the_filesystem_driver_never_lets_a_native_oserror_escape_from_put(tmp_path):
    root = tmp_path / "objects"
    store = FilesystemObjectStore(root)
    root.mkdir(parents=True)
    _unwritable(root)
    try:
        with pytest.raises(ObjectStoreError) as refused:
            store.put(key(), io.BytesIO(b"payload"))
    finally:
        _restore(root)
    _assert_safe(refused.value)


def test_the_filesystem_driver_never_lets_a_native_oserror_escape_from_open(tmp_path):
    """Objet present mais illisible: ce n'est pas une absence, c'est un echec de pilote."""
    root = tmp_path / "objects"
    store = FilesystemObjectStore(root)
    target = key()
    store.put(target, io.BytesIO(b"payload"))
    path = root.joinpath(*target.split("/"))
    _os.chmod(path, 0o000)
    try:
        with pytest.raises(ObjectStoreError) as refused:
            with store.open(target):
                pass
    finally:
        _os.chmod(path, 0o600)
    message = _assert_safe(refused.value)
    assert not isinstance(refused.value, ObjectNotFound), "illisible n'est pas introuvable"
    assert "lecture" in message


def test_a_genuinely_missing_filesystem_object_is_still_object_not_found(tmp_path):
    """Non-regression: la traduction ne doit pas avaler la semantique existante."""
    store = FilesystemObjectStore(tmp_path / "objects")
    with pytest.raises(ObjectNotFound):
        with store.open(key()):
            pass


# -- pilote S3 ----------------------------------------------------------------------------------

class _Refusing:
    """Client S3 qui refuse, comme botocore le fait: une SOUS-CLASSE portant `response`."""

    class Denied(Exception):
        def __init__(self, code: str) -> None:
            # botocore cite couramment le bucket dans le message: on verifie qu'il ne ressort pas
            super().__init__(f"An error occurred ({code}) when calling the operation: "
                             f"arn:aws:s3:::mervio-objects is denied via https://s3.amazonaws.com")
            self.response = {"Error": {"Code": code, "Message": "Access Denied"}}

    def __init__(self, code: str = "AccessDenied") -> None:
        self.code = code

    def upload_fileobj(self, *args, **kwargs):
        raise self.Denied(self.code)

    def get_object(self, **kwargs):
        raise self.Denied(self.code)

    def delete_object(self, **kwargs):
        raise self.Denied(self.code)


def _s3(code: str = "AccessDenied"):
    from mervio.storage.s3 import S3ObjectStore
    return S3ObjectStore("mervio-objects", client=_Refusing(code))


@pytest.mark.parametrize("operation", ["put", "open", "delete"])
def test_the_s3_driver_never_lets_a_provider_exception_escape(operation):
    store = _s3()
    target = key()
    with pytest.raises(ObjectStoreError) as refused:
        if operation == "put":
            store.put(target, io.BytesIO(b"payload"))
        elif operation == "delete":
            store.delete(target)
        else:
            with store.open(target):
                pass
    message = _assert_safe(refused.value)
    # le CODE du fournisseur est conserve; le message natif, qui cite le bucket et l'endpoint, non
    assert "AccessDenied" in message


def test_the_s3_driver_never_publishes_the_bucket_the_endpoint_or_the_arn():
    with pytest.raises(ObjectStoreError) as refused:
        _s3().delete(key())
    message = str(refused.value)
    for secretish in ("mervio-objects", "arn:aws", "s3.amazonaws.com", "https://"):
        assert secretish not in message, (secretish, message)


def test_a_missing_s3_key_is_still_object_not_found():
    """Non-regression: `NoSuchKey` garde sa semantique, il n'est pas noyé dans le contrat total."""
    with pytest.raises(ObjectNotFound):
        with _s3("NoSuchKey").open(key()):
            pass


def test_an_s3_delete_of_a_missing_key_stays_idempotent(s3_store):
    """Contre le vrai moto: detruire une cle absente reste un succes (D-056)."""
    s3_store.delete(key())


# -- capacite de destruction --------------------------------------------------------------------

def test_every_driver_reports_a_known_delete_capability(store):
    assert store.delete_capability() in DELETE_CAPABILITIES


def test_the_capability_of_each_driver_is_the_honest_one(memory_store, filesystem_store, s3_store):
    """Memoire: capable par construction. Systeme de fichiers: controle vivant. S3: indeterminable.

    S3 rend `undetermined` et NON `capable`: prouver `s3:DeleteObject` exigerait de l'appeler, et
    un drapeau declaratif deriverait de la politique IAM reelle (D-064 interdit ce faux positif).
    """
    assert memory_store.delete_capability() == DELETE_CAPABLE
    assert filesystem_store.delete_capability() == DELETE_CAPABLE
    assert s3_store.delete_capability() == DELETE_UNDETERMINED


def test_the_filesystem_capability_follows_the_real_root(tmp_path):
    """Pas un drapeau: le verdict change quand le montage change."""
    root = tmp_path / "objects"
    store = FilesystemObjectStore(root)
    root.mkdir(parents=True)
    assert store.delete_capability() == DELETE_CAPABLE
    _unwritable(root)
    try:
        assert store.delete_capability() == DELETE_INCAPABLE
    finally:
        _restore(root)
    assert store.delete_capability() == DELETE_CAPABLE


def test_the_filesystem_capability_answers_before_the_root_exists(tmp_path):
    """`put` cree la racine: on interroge le premier ancetre existant, la ou la creation aurait lieu."""
    store = FilesystemObjectStore(tmp_path / "not-created-yet" / "objects")
    assert store.delete_capability() == DELETE_CAPABLE
    _os.chmod(tmp_path, 0o500)
    try:
        assert store.delete_capability() == DELETE_INCAPABLE
    finally:
        _os.chmod(tmp_path, 0o700)


def test_the_capability_check_has_no_side_effect_whatsoever(store):
    """SANS EFFET DE BORD (D-064): ni sonde destructrice, ni cle creee, ni octet touche."""
    target, payload = key(), b"intact"
    store.put(target, io.BytesIO(payload))
    for _ in range(5):
        store.delete_capability()
    with store.open(target) as handle:
        assert handle.read() == payload


def test_the_filesystem_capability_check_creates_no_entry_under_the_root(tmp_path):
    root = tmp_path / "objects"
    store = FilesystemObjectStore(root)
    target = key()
    store.put(target, io.BytesIO(b"intact"))
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    for _ in range(5):
        assert store.delete_capability() == DELETE_CAPABLE
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before
