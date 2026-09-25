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
