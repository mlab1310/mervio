"""Tests de detection d'anomalies."""
from __future__ import annotations

from datetime import date

import pytest

from mervio.analytics.anomaly import DESIRABLE_DIRECTION, assess_direction, detect_anomalies
from mervio.analytics.periods import make_period
from mervio.analytics.timeseries import SeriesPoint
from mervio.config import AnomalyConfig

CFG = AnomalyConfig()


def series(values):
    anchor = date(2026, 6, 29)
    points = []
    for index, value in enumerate(values):
        period = make_period(date(anchor.year, anchor.month, anchor.day), "week")
        from datetime import timedelta
        period = make_period(anchor + timedelta(days=7 * index), "week")
        points.append(SeriesPoint(period, value))
    return points


def test_flat_series_yields_no_anomaly():
    assert detect_anomalies("revenue", series([100.0] * 10), CFG) == []


def test_small_variation_below_threshold_is_not_an_anomaly():
    assert detect_anomalies("revenue", series([100, 102, 98, 101, 99, 103, 100, 101, 105]), CFG) == []


def test_large_drop_is_detected():
    anomalies = detect_anomalies("revenue", series([100] * 8 + [60]), CFG)
    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.direction == "down"
    assert anomaly.delta_pct == pytest.approx(-0.40)
    assert anomaly.severity == "high"


def test_anomaly_always_states_its_criterion():
    anomaly = detect_anomalies("revenue", series([100] * 8 + [60]), CFG)[0]
    assert anomaly.criterion
    assert "15%" in anomaly.criterion and "materialite" in anomaly.criterion
    assert anomaly.evidence


def test_high_zscore_alone_cannot_create_an_anomaly():
    """Baseline tres stable: +4.5% donne z=3 mais reste sans portee business."""
    assert detect_anomalies("revenue", series([100, 102, 98, 101, 99, 103, 100, 101, 105]), CFG) == []


def test_zscore_still_escalates_a_material_variation():
    """-12% est materiel mais sous le seuil de 15%: le z-score doit le remonter."""
    found = detect_anomalies("revenue", series([100, 101, 99, 100, 101, 99, 100, 100, 88]), CFG)
    assert len(found) == 1 and found[0].z_score is not None


def test_zscore_computed_when_baseline_long_enough():
    anomaly = detect_anomalies("revenue", series([100, 105, 95, 102, 98, 101, 99, 100, 40]), CFG)[0]
    assert anomaly.z_score is not None
    assert "zscore" in anomaly.method


def test_zscore_absent_with_short_baseline():
    anomaly = detect_anomalies("revenue", series([100, 50]), CFG)[0]
    assert anomaly.z_score is None
    assert anomaly.method == "percentage_threshold_vs_rolling_mean"


def test_single_point_series_yields_nothing():
    assert detect_anomalies("revenue", series([100.0]), CFG) == []


def test_none_values_are_skipped():
    assert detect_anomalies("revenue", series([None, None, 100.0]), CFG) == []


def test_severity_scales_with_magnitude():
    medium = detect_anomalies("revenue", series([100] * 8 + [80]), CFG)[0]
    high = detect_anomalies("revenue", series([100] * 8 + [50]), CFG)[0]
    assert medium.severity in ("medium", "high")
    assert high.severity == "high"


def test_direction_assessment_refund_decrease_is_favorable():
    """Une BAISSE des remboursements est une bonne nouvelle, pas une alerte."""
    assert assess_direction("refunds", "down") == "favorable"
    assert assess_direction("refunds", "up") == "unfavorable"
    assert assess_direction("revenue", "down") == "unfavorable"
    assert assess_direction("ad_spend", "up") == "neutral"


def test_anomaly_carries_assessment():
    anomaly = detect_anomalies("refunds", series([100] * 8 + [40]), CFG)[0]
    assert anomaly.assessment == "favorable"


def test_every_series_metric_has_a_declared_direction():
    from mervio.analytics.pipeline import SERIES_METRICS
    for metric in SERIES_METRICS:
        assert metric in DESIRABLE_DIRECTION, f"direction non declaree pour {metric}"
