"""
Final end-to-end validation of Box Unfiltered/Filtered vs live EDGAR downloads.

Progress is tracked in testing_async_management.json (same shape as the
unfiltered scraper). Failures append to final_testing_failures.json.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path
from shutil import copyfileobj

import requests
from bs4 import BeautifulSoup

FINAL_TEST_DIR = Path(__file__).resolve().parent
ROOT_DIR = FINAL_TEST_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from box.box_upload import download_csv_safe, get_box_client
from check_filtered_failure import sec_zip_url
from helpers import increment, load_management, save_management
from ip_addresses import ip_range
from test_filtration.filtration_test_suite import (
    build_row_signature,
    default_target_checker,
    iter_csv_rows,
)

MANAGEMENT_PATH = FINAL_TEST_DIR / "testing_async_management.json"
FAILURES_PATH = FINAL_TEST_DIR / "final_testing_failures.json"

TEMP_EDGAR = FINAL_TEST_DIR / "temp_edgar.csv"
TEMP_UNFILTERED = FINAL_TEST_DIR / "temp_box_unfiltered.csv"
TEMP_FILTERED = FINAL_TEST_DIR / "temp_box_filtered.csv"

UNFILTERED_FOLDER = "EDGAR log files/Unfiltered"
FILTERED_FOLDER = "EDGAR log files/Filtered"

HEADERS = {
    "User-Agent": "Choi_Research - jcahn1@lion.lmu.edu",
}

LOG_DATE_RE = re.compile(r"log(\d{4})(\d{2})(\d{2})", re.IGNORECASE)
ORG_COLUMN = "organization"


def human_readable(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024


def _cleanup(*paths: Path) -> None:
    # Remove local temp files from this day's run.
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


def log_date_from_name(name: str) -> date | None:
    # Parse logYYYYMMDD from a zip/csv filename or URL.
    match = LOG_DATE_RE.search(name)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def normalize_sec_zip_url(link: str) -> str:
    """
    Rebuild the SEC zip URL with the correct quarter for the log date.
    Keeps downloads consistent even if the index href uses a wrong Qtr path.
    """
    log_date = log_date_from_name(link)
    if log_date is None:
        return link
    return sec_zip_url(log_date.year, log_date.month, log_date.day)


def csv_name_from_link(link: str) -> str:
    # Convert .../logYYYYMMDD.zip -> logYYYYMMDD.csv for Box lookups.
    zip_name = link.rsplit("/", 1)[-1]
    if zip_name.lower().endswith(".zip"):
        return zip_name[:-4] + ".csv"
    if zip_name.lower().endswith(".csv"):
        return zip_name
    log_date = log_date_from_name(link)
    if log_date is None:
        raise ValueError(f"Could not derive CSV name from link: {link}")
    return f"log{log_date.year}{log_date.month:02d}{log_date.day:02d}.csv"


def log_failure(year: str, filename: str, reason: str) -> None:
    """Append one failure (always includes filename) under the year bucket."""
    if FAILURES_PATH.exists():
        data = json.loads(FAILURES_PATH.read_text(encoding="utf-8") or "{}")
    else:
        data = {"failures": {}}

    failures = data.setdefault("failures", {})
    year_list = failures.setdefault(str(year), [])
    year_list.append({"filename": filename, "reason": reason})

    FAILURES_PATH.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"    logged failure -> {FAILURES_PATH.name}: {filename}")


def scrape_edgar_csv(link: str, output_path: Path) -> Path:
    """
    Download one EDGAR zip and extract its CSV to output_path.
    Raises requests HTTPError on 403/503/etc.
    """
    # Fetch the zip into memory, then extract the first CSV member.
    response = requests.get(link, headers=HEADERS)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("No CSV file found in zip archive")
        with archive.open(csv_names[0]) as csv_file, open(output_path, "wb") as out_file:
            copyfileobj(csv_file, out_file)

    print(f"    edgar saved {human_readable(output_path.stat().st_size)} -> {output_path.name}")
    return output_path


def compare_unfiltered_row_by_row(
    edgar_path: Path,
    box_unfiltered_path: Path,
) -> str | None:
    """
    Stream both CSVs in lockstep; each row/column must match.
    Returns a reason string with line number on first mismatch, else None.
    """
    with (
        open(edgar_path, "r", encoding="utf-8", newline="") as edgar_f,
        open(box_unfiltered_path, "r", encoding="utf-8", newline="") as box_f,
    ):
        edgar_reader = csv.DictReader(edgar_f)
        box_reader = csv.DictReader(box_f)

        edgar_fields = list(edgar_reader.fieldnames or [])
        box_fields = list(box_reader.fieldnames or [])
        # Header must match before comparing body rows.
        if edgar_fields != box_fields:
            return (
                "unfiltered did not match - line 1 (header) - "
                f"unmatching value currently in box: {box_fields!r}, "
                f"from edgar: {edgar_fields!r}"
            )

        line_number = 1  # header is line 1; data starts at 2
        for edgar_row, box_row in zip(edgar_reader, box_reader):
            line_number += 1
            for column in edgar_fields:
                edgar_val = "" if edgar_row.get(column) is None else str(edgar_row.get(column))
                box_val = "" if box_row.get(column) is None else str(box_row.get(column))
                if edgar_val != box_val:
                    return (
                        f"unfiltered did not match - line {line_number} - "
                        f"unmatching value currently in box: {box_val!r}, "
                        f"from edgar: {edgar_val!r}"
                    )

        # Detect length mismatch after zip exhausts the shorter file.
        extra_edgar = next(edgar_reader, None)
        extra_box = next(box_reader, None)
        if extra_edgar is not None:
            return (
                f"unfiltered did not match - line {line_number + 1} - "
                "unmatching value currently in box: <missing row>, "
                f"from edgar: {dict(extra_edgar)!r}"
            )
        if extra_box is not None:
            return (
                f"unfiltered did not match - line {line_number + 1} - "
                f"unmatching value currently in box: {dict(extra_box)!r}, "
                "from edgar: <missing row>"
            )

    return None


def strip_organization(row: dict[str, str]) -> dict[str, str]:
    # Drop the filtered-only organization column for membership/completeness matching.
    return {key: value for key, value in row.items() if key != ORG_COLUMN}


def run_filtered_membership_completeness(
    unfiltered_path: Path,
    filtered_path: Path,
) -> list[str]:
    """
    Membership + completeness using Box unfiltered as original and Box filtered
    as filtered. Organization is stripped in-memory before row matching.
    Returns a list of failure reason strings (may be empty).
    """
    reasons: list[str] = []

    # Use unfiltered headers as the shared compare columns (no organization).
    with open(unfiltered_path, "r", encoding="utf-8", newline="") as file:
        compare_columns = list(csv.DictReader(file).fieldnames or [])

    # Membership: every filtered row (org stripped) must still be a target IP.
    line_number = 1
    for row in iter_csv_rows(filtered_path):
        line_number += 1
        stripped = strip_organization(row)
        ip_value = (stripped.get("ip") or "").strip()
        if not ip_value or not default_target_checker(ip_value):
            reasons.append(
                f"filtered membership failed - line {line_number} - "
                f"unmatching value currently in box: ip={ip_value!r} "
                f"(not in target set), from edgar/check: expected target IP"
            )
            break

    # Completeness: every target row in Box unfiltered must appear in filtered.
    expected: Counter[str] = Counter()
    for row in iter_csv_rows(unfiltered_path):
        ip_value = (row.get("ip") or "").strip()
        if ip_value and default_target_checker(ip_value):
            expected[build_row_signature(row, compare_columns)] += 1

    observed: Counter[str] = Counter()
    for row in iter_csv_rows(filtered_path):
        observed[build_row_signature(strip_organization(row), compare_columns)] += 1

    missing = 0
    unexpected = 0
    for signature, count in expected.items():
        if observed.get(signature, 0) < count:
            missing += count - observed.get(signature, 0)
    for signature, count in observed.items():
        if expected.get(signature, 0) < count:
            unexpected += count - expected.get(signature, 0)

    if missing or unexpected:
        # Completeness is aggregate; cite counts rather than a single line.
        reasons.append(
            "filtered completeness failed - "
            f"missing_row_count={missing}, unexpected_row_count={unexpected}"
        )

    return reasons


def run_organization_categorization_check(filtered_path: Path) -> list[str]:
    """
    Recompute ip_range(ip) for each filtered row and compare to organization.
    Returns reason strings with line numbers for mismatches.
    """
    reasons: list[str] = []
    line_number = 1
    for row in iter_csv_rows(filtered_path):
        line_number += 1
        ip_value = (row.get("ip") or "").strip()
        stored_org = (row.get(ORG_COLUMN) or "").strip()
        # Re-run the same categorization used when filtering was produced.
        expected_org = ip_range(ip_value)
        expected_text = "" if expected_org is None else expected_org
        if expected_text != stored_org:
            reasons.append(
                f"filtered organization miscategorized - line {line_number} - "
                f"unmatching value currently in box: {stored_org!r}, "
                f"from edgar/check: {expected_text!r}"
            )
            # Log all org mismatches for this file (not only the first).
    return reasons


def process_one_day(link: str, year: str, client):
    """
    Download EDGAR + Box Unfiltered + Box Filtered, run unfiltered then filtered
    checks, and log any failures. Returns the (possibly refreshed) Box client.
    """
    filename = csv_name_from_link(link)
    sec_link = normalize_sec_zip_url(link)
    print(f"    filename={filename}")
    print(f"    sec_link={sec_link}")

    # --- EDGAR download ---
    try:
        scrape_edgar_csv(sec_link, TEMP_EDGAR)
    except requests.HTTPError as error:
        status = getattr(error.response, "status_code", "?")
        log_failure(
            year,
            filename,
            f"edgar: HTTP {status} for {sec_link}",
        )
        _cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client
    except Exception as error:
        log_failure(year, filename, f"edgar: {type(error).__name__}: {error}")
        _cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    # --- Box Unfiltered download ---
    unfiltered_path, box_reason, client = download_csv_safe(
        filename,
        client=client,
        destination_folder=UNFILTERED_FOLDER,
        local_path=TEMP_UNFILTERED,
    )
    if unfiltered_path is None:
        log_failure(year, filename, box_reason or "box: unfiltered download failed")
        _cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    # --- Box Filtered download (full file including organization column) ---
    filtered_path, box_reason, client = download_csv_safe(
        filename,
        client=client,
        destination_folder=FILTERED_FOLDER,
        local_path=TEMP_FILTERED,
    )
    if filtered_path is None:
        log_failure(year, filename, box_reason or "box: filtered download failed")
        _cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    # --- Unfiltered row/column equality ---
    unfiltered_reason = compare_unfiltered_row_by_row(TEMP_EDGAR, TEMP_UNFILTERED)
    if unfiltered_reason is not None:
        log_failure(year, filename, unfiltered_reason)
        _cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    print("    unfiltered compare: passed")

    # --- Filtered membership + completeness (org stripped in-memory) ---
    for reason in run_filtered_membership_completeness(TEMP_UNFILTERED, TEMP_FILTERED):
        log_failure(year, filename, reason)

    # --- Filtered organization re-categorization ---
    for reason in run_organization_categorization_check(TEMP_FILTERED):
        log_failure(year, filename, reason)

    print("    filtered checks: finished")
    _cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
    return client


def _advance_or_complete(year: str) -> bool:
    """Advance testing_async_management.json index/year. True when all done."""
    # Same year-completion logic as webscraper_unfiltered, but on the test path.
    completion = increment(path=MANAGEMENT_PATH)
    if completion != "complete":
        return False

    print(f"=== Year {year}: complete ===")
    next_year = str(int(year) + 1)
    task_management = load_management(path=MANAGEMENT_PATH)
    if next_year in task_management["baseline_links"]:
        task_management["current"] = {
            "current_index": 0,
            "year": next_year,
            "links": [],
        }
        save_management(task_management, path=MANAGEMENT_PATH)
        print(f"=== Advancing to year {next_year} ===")
        return False

    print("=== All years complete ===")
    return True


def run_final_tests(client) -> None:
    """Year-by-year loop driven by testing_async_management.json."""
    while True:
        task_management = load_management(path=MANAGEMENT_PATH)
        current = task_management["current"]
        year = current["year"]

        # Fetch this year's SEC index once, then store zip links for resume.
        if not current.get("links"):
            #non-2017 block:
            if not year == "2017":
                print(f"=== Year {year}: fetching file list ===")
                base_url = task_management["baseline_links"][year]
                html = requests.get(base_url, headers=HEADERS).text
                parsed_html = BeautifulSoup(html, "html.parser")
                links = [elmt["href"] for elmt in parsed_html.find_all("a", href=True)]
                # Keep only zip download links when present.
                zip_links = [link for link in links if str(link).lower().endswith(".zip")]
                current["links"] = zip_links or links
                current["current_index"] = 0
                save_management(task_management, path=MANAGEMENT_PATH)
                print(f"=== Year {year}: {len(current['links'])} files to process ===")
                continue
            else:
                print(f"=== Year {year}: fetching file list ===")
                links = []
                base_url = task_management["baseline_links"][year]
                html = requests.get(base_url, headers=HEADERS).text
                parsed_html = BeautifulSoup(html, "html.parser")
                links = [elmt["href"] for elmt in parsed_html.find_all("a", href=True)]
                
                #for 2017,only test until june 30th
                links = links[:181]
                # Keep only zip download links when present.
                zip_links = [link for link in links if str(link).lower().endswith(".zip")]
                current["links"] = zip_links or links
                current["current_index"] = 0
                save_management(task_management, path=MANAGEMENT_PATH)
                print(f"=== Year {year}: {len(current['links'])} files to process ===")
                continue


        links = current["links"]
        index = current["current_index"]
        link = links[index]
        file_name = link.rsplit("/", 1)[-1]
        print(f"[{year}] file {index + 1}/{len(links)}: {file_name}")

        # Run EDGAR/Box validation for this day; failures are logged, not fatal.
        client = process_one_day(link, year, client)

        if _advance_or_complete(year):
            break


if __name__ == "__main__":
    box_client = get_box_client()
    run_final_tests(box_client)
