"""Record shape contracts — the bridge between fixtures and live data.

The single most likely real-world break is a data provider renaming a JSON field,
which silently yields ``None`` values or zero parsed rows. ``scripts/validate_live.py``
parses LIVE data and runs these same assertions the fixtures satisfy; any mismatch
fails loudly with the offending field, rather than quietly poisoning the corridor.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any

# Fields that MUST be present and non-null for each record type to be usable.
REQUIRED_NON_NULL: dict[str, tuple[str, ...]] = {
    "ForwardEstimateRecord": (
        "ticker",
        "as_of_date",
        "period_type",
        "fiscal_period",
        "period_end_date",
        "metric",
        "value",
        "source",
        "basis",
        "currency",
        "construction_method",
    ),
    "PriceRecord": ("ticker", "price_date", "close", "source", "currency", "split_ratio"),
    "FundamentalRecord": (
        "ticker",
        "fiscal_period",
        "period_end_date",
        "value",
        "source",
        "basis",
    ),
}


class ShapeMismatch(AssertionError):
    """Raised when parsed records do not match the expected schema."""


def assert_records_shape(records: list[Any], expect_non_empty: bool = True) -> None:
    """Assert a list of parsed records matches its required-field contract.

    Checks: the list is a homogeneous list of a known dataclass record type,
    (optionally) non-empty, and every record has all its REQUIRED_NON_NULL fields
    present and not None. Raises ShapeMismatch naming the first offending field.
    """
    if expect_non_empty and not records:
        raise ShapeMismatch("parsed ZERO records — a field rename or empty response is likely")
    if not records:
        return
    first = records[0]
    if not is_dataclass(first):
        raise ShapeMismatch(f"records are not dataclasses: got {type(first).__name__}")
    cls_name = type(first).__name__
    required = REQUIRED_NON_NULL.get(cls_name)
    if required is None:
        raise ShapeMismatch(f"no shape contract registered for {cls_name}")
    field_names = {f.name for f in fields(first)}
    missing_fields = [f for f in required if f not in field_names]
    if missing_fields:
        raise ShapeMismatch(f"{cls_name} missing fields entirely: {missing_fields}")
    for i, rec in enumerate(records):
        for fname in required:
            if getattr(rec, fname) is None:
                raise ShapeMismatch(
                    f"{cls_name}[{i}].{fname} is None — likely a renamed/absent provider field"
                )
