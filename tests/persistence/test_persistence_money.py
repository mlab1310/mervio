"""Montants: numeric(19,4), aller-retour float exact, refus explicite de toute perte."""
from __future__ import annotations

import random
from decimal import Decimal

import psycopg
import pytest
from psycopg.types.numeric import FloatLoader

from mervio.persistence.errors import MoneyPrecisionError
from mervio.persistence.money import money_from_db, money_to_db, optional_money_to_db


@pytest.mark.parametrize("value, text", [
    (0.0, "0.0"), (89.99, "89.99"), (-12.34, "-12.34"), (0.0001, "0.0001"), (100.0, "100.0"),
    (123456789012345.0, "123456789012345.0"), (1e-4, "0.0001"), (12.5e-3, "0.0125"),
])
def test_representable_amounts_are_sent_as_exact_decimal_text(value, text):
    assert Decimal(money_to_db(value, field="x")) == Decimal(text)


@pytest.mark.parametrize("value", [
    12.34567, 5e-05, 0.1 + 0.2, 1e15, -1e15, 1234567890123456.0, float("inf"), float("-inf"), float("nan"), -0.0, 1e-7,
])
def test_amounts_that_would_lose_information_are_refused_never_rounded(value):
    with pytest.raises(MoneyPrecisionError):
        money_to_db(value, field="orders.subtotal")


@pytest.mark.parametrize("value", [10, True, Decimal("1.00"), "1.00", None])
def test_non_float_amounts_are_refused(value):
    with pytest.raises(MoneyPrecisionError):
        money_to_db(value, field="orders.total")


def test_optional_amount_keeps_none_as_null():
    assert optional_money_to_db(None, field="payments.fee") is None
    assert optional_money_to_db(1.5, field="payments.fee") == "1.5"


def test_error_message_names_the_field_but_never_the_value():
    with pytest.raises(MoneyPrecisionError) as caught:
        money_to_db(987.654321, field="refunds.amount")
    assert "refunds.amount" in str(caught.value) and "987" not in str(caught.value)


def test_numeric_roundtrip_through_postgresql_is_bit_exact(owner):
    rng = random.Random(4101)
    values = [round(rng.uniform(-1e6, 1e6), 2) for _ in range(3000)]
    values += [round(rng.uniform(-10, 10), 4) for _ in range(3000)]
    values += [0.01, 0.1, 0.3, 19.99, 999999999999999.9999, -999999999999999.9999, 0.0005, 1.0005]
    texts = []
    for value in values:
        try:
            texts.append((value, money_to_db(value, field="x")))
        except MoneyPrecisionError:
            # 999999999999999.9999 n'a pas de float exact a 4 decimales: refuse, c'est le contrat
            assert Decimal(repr(value)) != Decimal(repr(value)).quantize(Decimal("0.0001")) or abs(value) >= 1e15
    with owner.cursor() as cur:
        cur.execute("CREATE TEMP TABLE money_probe (i int, amount numeric(19,4))")
        cur.execute("INSERT INTO money_probe SELECT * FROM unnest(%s::int[], %s::numeric[])",
                    (list(range(len(texts))), [t for _, t in texts]))
        cur.execute("SELECT amount FROM money_probe ORDER BY i")
        as_decimal = [row[0] for row in cur.fetchall()]
    with owner.cursor() as cur:
        cur.adapters.register_loader("numeric", FloatLoader)
        cur.execute("SELECT amount FROM money_probe ORDER BY i")
        as_float = [row[0] for row in cur.fetchall()]
    for (value, _), decimal_value, float_value in zip(texts, as_decimal, as_float):
        assert money_from_db(decimal_value) == value
        assert float_value == value and type(float_value) is float
        assert repr(float_value) == repr(value)


def test_database_rejects_amounts_beyond_numeric_19_4(owner):
    owner.execute("CREATE TEMP TABLE money_limit (amount numeric(19,4))")
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        owner.execute("INSERT INTO money_limit VALUES ('1000000000000000')")
