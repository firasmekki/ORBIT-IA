import math

import pytest

from app.agent.stat_engine import calculate_series_statistics


def test_calculates_real_series_statistics_and_quality():
    stats = calculate_series_statistics(
        [
            {"month": "Jan", "kwh": 1},
            {"month": "Feb", "kwh": 2},
            {"month": "Mar", "kwh": 3},
            {"month": "Apr", "kwh": 100},
        ],
        [{"field": "kwh", "unit": "kWh"}],
    )["kwh"]
    assert stats["count"] == 4
    assert stats["mean"] == pytest.approx(26.5)
    assert stats["median"] == 2.5
    assert stats["min"] == 1 and stats["max"] == 100
    assert stats["quality"] == 1
    assert stats["iqr_outlier_count"] == 1


def test_missing_booleans_and_non_finite_values_are_not_coerced():
    stats = calculate_series_statistics(
        [{"v": 2}, {"v": None}, {"v": True}, {"v": math.inf}], [{"field": "v"}]
    )["v"]
    assert stats["count"] == 1
    assert stats["missing_count"] == 3
    assert stats["mean"] == 2
    assert stats["warnings"]


def test_rejects_unbounded_input():
    with pytest.raises(ValueError, match="10,000"):
        calculate_series_statistics([{}] * 10_001, [{"field": "v"}])
