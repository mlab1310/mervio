"""Identite client a cle (Mission 004.4.2, D-053, D-058). Sans base.

Aucune cle litterale: tout materiel de cle est tire a l'execution.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import pickle
import secrets
import uuid

import pytest

from mervio.identity import (
    REF_PATTERN, SALT_BYTES, CustomerIdentity, IdentityKeyError, MasterKey, new_salt, normalize_email,
)


@pytest.fixture
def master():
    return MasterKey(secrets.token_bytes(32))


def test_references_have_the_contract_format_and_carry_no_email():
    identity = CustomerIdentity.ephemeral()
    email_ref, guest_ref = identity.ref_email("alice@example.com"), identity.ref_guest("#1001")
    assert REF_PATTERN.match(email_ref) and email_ref.startswith("c1:") and len(email_ref) == 35
    assert REF_PATTERN.match(guest_ref) and guest_ref.startswith("g1:") and len(guest_ref) == 35
    for ref in (email_ref, guest_ref):
        assert "@" not in ref and "alice" not in ref and "1001" not in ref


def test_the_reference_is_the_first_128_bits_of_a_keyed_hmac_of_the_normalized_email():
    key = secrets.token_bytes(32)
    expected = hmac.new(key, b"email:alice@example.com", hashlib.sha256).hexdigest()[:32]
    assert CustomerIdentity(key).ref_email("  Alice@Example.COM ") == "c1:" + expected
    guest = hmac.new(key, b"guest_order:#42", hashlib.sha256).hexdigest()[:32]
    assert CustomerIdentity(key).ref_guest(" #42 ") == "g1:" + guest


def test_references_are_stable_for_a_key_and_differ_across_keys():
    key = secrets.token_bytes(32)
    first, again, other = CustomerIdentity(key), CustomerIdentity(key), CustomerIdentity.ephemeral()
    assert first.ref_email("a@x.com") == again.ref_email("A@X.COM") == first.ref_email("a@x.com")
    assert first.ref_email("a@x.com") != other.ref_email("a@x.com")
    assert first.ref_email("a@x.com") != first.ref_email("b@x.com")


def test_the_email_and_guest_namespaces_never_collide():
    identity = CustomerIdentity.ephemeral()
    assert identity.ref_email("x@y.z")[3:] != identity.ref_guest("x@y.z")[3:]


def test_the_connector_rule_is_email_then_guest():
    identity = CustomerIdentity.ephemeral()
    assert identity.ref_customer("A@X.com", "#1") == identity.ref_email("a@x.com")
    assert identity.ref_customer(None, "#1") == identity.ref_customer("  ", "#1") == identity.ref_guest("#1")
    assert normalize_email("  B@X.COM ") == "b@x.com" and normalize_email("") is None


@pytest.mark.parametrize("call", [lambda i: i.ref_email(""), lambda i: i.ref_email(None), lambda i: i.ref_guest(" ")])
def test_empty_identities_are_refused(call):
    with pytest.raises(IdentityKeyError):
        call(CustomerIdentity.ephemeral())


def test_organization_keys_depend_on_master_organization_and_salt(master):
    organization, salt = uuid.uuid4(), new_salt()
    assert len(salt) == SALT_BYTES
    reference = master.organization_identity(organization, salt).ref_email("a@x.com")
    assert master.organization_identity(organization, salt).ref_email("a@x.com") == reference  # deterministe
    assert master.organization_identity(uuid.uuid4(), salt).ref_email("a@x.com") != reference
    assert master.organization_identity(organization, new_salt()).ref_email("a@x.com") != reference
    other_master = MasterKey(secrets.token_bytes(32))
    assert other_master.organization_identity(organization, salt).ref_email("a@x.com") != reference


def test_the_organization_key_follows_the_documented_derivation(master):
    raw, organization, salt = secrets.token_bytes(32), uuid.uuid4(), new_salt()
    key = hmac.new(raw, b"mervio:identity:v1:" + str(organization).encode() + b":" + salt, hashlib.sha256).digest()
    assert MasterKey(raw).organization_identity(organization, salt).ref_guest("#1") == CustomerIdentity(key).ref_guest("#1")


def test_key_identifiers_are_short_stable_non_secret_and_distinct(master):
    raw = secrets.token_bytes(32)
    assert MasterKey(raw).key_id == MasterKey(raw).key_id
    assert len(master.key_id) == 16 and int(master.key_id, 16) >= 0
    assert master.key_id != MasterKey(secrets.token_bytes(32)).key_id
    assert raw.hex()[:16] != MasterKey(raw).key_id  # ce n'est pas un prefixe de la cle
    identity = CustomerIdentity.ephemeral()
    assert len(identity.key_id) == 16 and identity.key_id not in identity.export_hex()


@pytest.mark.parametrize("bad", [b"", b"x" * 31, "0" * 64])
def test_short_or_untyped_keys_are_refused(bad):
    with pytest.raises(IdentityKeyError):
        MasterKey(bad)
    with pytest.raises(IdentityKeyError):
        CustomerIdentity(bad)


@pytest.mark.parametrize("salt", [b"", b"s" * 31, b"s" * 33, "s" * 32])
def test_a_salt_must_be_exactly_32_bytes(master, salt):
    with pytest.raises(IdentityKeyError):
        master.organization_identity(uuid.uuid4(), salt)


def test_key_material_is_never_displayed_nor_serialized(master):
    identity = master.organization_identity(uuid.uuid4(), new_salt())
    secret_hex = identity.export_hex()
    for obj in (master, identity):
        assert "[redacted]" in repr(obj) and "[redacted]" in str(obj) and f"{obj}" == str(obj)
        assert secret_hex not in repr(obj)
        with pytest.raises(TypeError):
            pickle.dumps(obj)
        with pytest.raises(TypeError):
            copy.deepcopy(obj)


def test_an_explicit_key_file_gives_reproducible_references(tmp_path):
    key_hex = secrets.token_hex(32)
    path = tmp_path / "identity.key"
    path.write_text(key_hex + "\n", encoding="utf-8")
    assert (CustomerIdentity.from_key_file(path).ref_email("a@x.com")
            == CustomerIdentity(bytes.fromhex(key_hex)).ref_email("a@x.com"))


@pytest.mark.parametrize("content", ["", "zz" * 32, "ab" * 31, "abc" * 21 + "ab"])
def test_a_malformed_key_file_is_refused_without_its_content(tmp_path, content):
    path = tmp_path / "identity.key"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(IdentityKeyError) as error:
        CustomerIdentity.from_key_file(path)
    assert (content or "@@") not in str(error.value)
    with pytest.raises(IdentityKeyError):
        CustomerIdentity.from_key_file(tmp_path / "absent.key")


def test_the_master_key_parses_from_hexadecimal_only():
    key_hex = secrets.token_hex(32)
    assert MasterKey.from_hex(key_hex) == MasterKey(bytes.fromhex(key_hex))
    with pytest.raises(IdentityKeyError) as error:
        MasterKey.from_hex("g" * 64)
    assert "g" * 64 not in str(error.value)


def test_references_are_memoized_per_distinct_identity():
    identity = CustomerIdentity.ephemeral()
    refs = [identity.ref_email(f"client{i % 10}@x.com") for i in range(1000)]
    assert len(set(refs)) == 10
    assert len(identity._memo) == 10  # un calcul par client distinct, pas par commande


# -- F-08: provenance de la cle; le repli ephemere reste reserve aux outils locaux --------------

def test_every_key_carries_its_provenance():
    master = MasterKey(secrets.token_bytes(32))
    organization = uuid.uuid4()
    org = master.organization_identity(organization, secrets.token_bytes(32))
    assert (org.origin, org.organization_id) == ("organization", organization)
    assert (CustomerIdentity.ephemeral().origin, CustomerIdentity.ephemeral().organization_id) == ("ephemeral", None)
    assert (CustomerIdentity(secrets.token_bytes(32)).origin) == "explicit"


def test_the_explicit_key_file_is_a_local_key_even_with_the_organization_material(tmp_path):
    org = MasterKey(secrets.token_bytes(32)).organization_identity(uuid.uuid4(), secrets.token_bytes(32))
    key = tmp_path / "k"
    key.write_text(org.export_hex(), encoding="utf-8")
    local = CustomerIdentity.from_key_file(key)
    assert local.key_id == org.key_id and (local.origin, local.organization_id) == ("explicit", None)


def test_the_local_helpers_stamp_the_key_they_used(sample_paths):
    """Le repli ephemere historique de `load_dataset` reste pour les outils locaux, mais le jeu porte
    l'identifiant de SA cle: la persistance le reconnait et le refuse (tests PostgreSQL F-08)."""
    from mervio.analytics.pipeline import load_dataset
    explicit = CustomerIdentity.ephemeral()
    assert load_dataset(sample_paths, explicit).identity_key_id == explicit.key_id
    a, b = load_dataset(sample_paths), load_dataset(sample_paths)
    assert a.identity_key_id and b.identity_key_id and a.identity_key_id != b.identity_key_id
    assert a.identity_key_id != explicit.key_id
    window = a.window(min(o.created_at for o in a.orders), max(o.created_at for o in a.orders))
    assert window.identity_key_id == a.identity_key_id
