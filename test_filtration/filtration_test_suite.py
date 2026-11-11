from __future__ import annotations

import csv
import hashlib
from collections import Counter
from pathlib import Path
from typing import Callable, Iterator

from ip_addresses import ip_range


Row = dict[str, str]
TargetChecker = Callable[[str], bool]


def default_target_checker(ip_value: str) -> bool:
    """Return True when an IP belongs to one of the target organizations."""
    return ip_range(ip_value) is not None


def _normalize_value(value: object) -> str:
    if value is None:
        return ""
    return str(value)


def iter_csv_rows(path: str | Path) -> Iterator[Row]:
    """Yield rows from a CSV file without loading the whole file into memory."""
    resolved_path = Path(path)
    if resolved_path.suffix.lower() != ".csv":
        raise ValueError(f"Unsupported file type for {resolved_path}. Expected .csv")

    with open(resolved_path, "r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            yield dict(row)


def load_csv_rows(path: str | Path) -> list[Row]:
    """Load all rows from a CSV file (avoid for large EDGAR logs)."""
    return list(iter_csv_rows(path))


def build_row_signature(row: Row, columns: list[str]) -> str:
    """
    Build a stable hash for a row using only the supplied columns.

    This supports duplicate-safe completeness checks using Counter.
    """
    joined = "\x1f".join(_normalize_value(row.get(column)) for column in columns)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def membership_check(
    filtered_rows: Iterator[Row] | list[Row],
    ip_column: str = "ip",
    target_checker: TargetChecker = default_target_checker,
) -> dict[str, object]:
    """
    Verify every row in the filtered CSV belongs to the target IP set.
    Streams rows; only keeps a small sample of invalid IPs.
    """
    invalid_rows = 0
    invalid_ips: list[str] = []

    for row in filtered_rows:
        ip_value = _normalize_value(row.get(ip_column)).strip()
        if not ip_value or not target_checker(ip_value):
            invalid_rows += 1
            if len(invalid_ips) < 10:
                invalid_ips.append(ip_value)

    return {
        "passed": invalid_rows == 0,
        "invalid_row_count": invalid_rows,
        "sample_invalid_ips": invalid_ips,
    }


def completeness_check(
    original_path: str | Path,
    filtered_path: str | Path,
    ip_column: str = "ip",
    target_checker: TargetChecker = default_target_checker,
    compare_columns: list[str] | None = None,
) -> dict[str, object]:
    """
    Ensure every matching row from the original CSV appears in the filtered CSV,
    accounting for duplicate rows.

    Streams both files so only matching-row signature counters are held in memory
    (typically tiny for FTC/DOJ filters), not the full raw CSV.
    """
    if compare_columns is None:
        compare_columns = []
        with open(original_path, "r", encoding="utf-8", newline="") as file:
            reader = csv.DictReader(file)
            compare_columns = list(reader.fieldnames or [])

    expected_signatures: Counter[str] = Counter()
    for row in iter_csv_rows(original_path):
        ip_value = _normalize_value(row.get(ip_column)).strip()
        if ip_value and target_checker(ip_value):
            expected_signatures[build_row_signature(row, compare_columns)] += 1

    observed_signatures: Counter[str] = Counter()
    for row in iter_csv_rows(filtered_path):
        # Filtered rows include an extra "organization" column; signatures use
        # original columns only so they match expected hashes.
        observed_signatures[build_row_signature(row, compare_columns)] += 1

    missing_rows = 0
    unexpected_rows = 0
    missing_signature_count = 0
    unexpected_signature_count = 0

    for signature, expected_count in expected_signatures.items():
        observed_count = observed_signatures.get(signature, 0)
        if observed_count < expected_count:
            missing_signature_count += 1
            missing_rows += expected_count - observed_count

    for signature, observed_count in observed_signatures.items():
        expected_count = expected_signatures.get(signature, 0)
        if observed_count > expected_count:
            unexpected_signature_count += 1
            unexpected_rows += observed_count - expected_count

    return {
        "passed": missing_rows == 0 and unexpected_rows == 0,
        "missing_row_count": missing_rows,
        "unexpected_row_count": unexpected_rows,
        "missing_signature_count": missing_signature_count,
        "unexpected_signature_count": unexpected_signature_count,
        "compare_columns": compare_columns,
    }


def run_filtration_test_suite(
    original_csv_file: str | Path,
    filtered_csv_file: str | Path,
    ip_column: str = "ip",
    target_checker: TargetChecker = default_target_checker,
    compare_columns: list[str] | None = None,
) -> dict[str, object]:
    """
    Run both CSV filtration validations and return structured pass/fail results.

    Streams large files instead of loading them fully into memory.

    Returns:
    - overall_passed
    - membership_check
    - completeness_check
    """
    membership_results = membership_check(
        filtered_rows=iter_csv_rows(filtered_csv_file),
        ip_column=ip_column,
        target_checker=target_checker,
    )
    completeness_results = completeness_check(
        original_path=original_csv_file,
        filtered_path=filtered_csv_file,
        ip_column=ip_column,
        target_checker=target_checker,
        compare_columns=compare_columns,
    )

    return {
        "overall_passed": membership_results["passed"] and completeness_results["passed"],
        "membership_check": membership_results,
        "completeness_check": completeness_results,
    }


if __name__ == "__main__":
    raise SystemExit(
        "Import and call run_filtration_test_suite(original_csv_file, filtered_csv_file, ...)"
    )
