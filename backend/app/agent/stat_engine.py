"""Deterministic statistics for already-authorized, structured data."""

import math
from statistics import median, stdev


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def calculate_series_statistics(rows: list[dict], value_fields: list[dict]) -> dict[str, dict]:
    """Summarize numeric columns without asking the LLM to do arithmetic.

    Callers must pass rows from an ACL-filtered source or values explicitly
    supplied by the user. Invalid/missing values are reported, never coerced
    to zero. A hard row limit bounds CPU and memory for the chart path.
    """
    if len(rows) > 10_000:
        raise ValueError("statistics input exceeds 10,000 rows")
    results: dict[str, dict] = {}
    for field_spec in value_fields:
        field = field_spec.get("field")
        if not isinstance(field, str) or not field:
            raise ValueError("each value field needs a non-empty field name")
        values: list[float] = []
        for row in rows:
            value = row.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                try:
                    numeric = float(value)
                except (OverflowError, ValueError):
                    continue
                if math.isfinite(numeric):
                    values.append(numeric)
        missing = len(rows) - len(values)
        if not values:
            results[field] = {
                "unit": field_spec.get("unit"), "count": 0, "missing_count": missing,
                "quality": 0.0, "warnings": ["Aucune valeur numérique exploitable."],
            }
            continue

        q25 = _quantile(values, 0.25)
        q75 = _quantile(values, 0.75)
        mean = math.fsum(values) / len(values)
        iqr = q75 - q25
        lower, upper = q25 - 1.5 * iqr, q75 + 1.5 * iqr
        warnings = ["Certaines lignes sont manquantes ou non numériques."] if missing else []
        results[field] = {
            "unit": field_spec.get("unit"),
            "count": len(values),
            "missing_count": missing,
            "mean": mean,
            "median": median(values),
            "min": min(values),
            "max": max(values),
            "variance": (math.fsum((value - mean) ** 2 for value in values) / len(values)),
            "stddev_sample": stdev(values) if len(values) > 1 else None,
            "q25": q25,
            "q75": q75,
            "first_to_last_change": values[-1] - values[0] if len(values) > 1 else None,
            "iqr_outlier_count": sum(value < lower or value > upper for value in values),
            "quality": len(values) / len(rows) if rows else 0.0,
            "warnings": warnings,
        }
    return results
