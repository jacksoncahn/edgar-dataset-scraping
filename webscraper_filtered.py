import csv
import io
import zipfile
from pathlib import Path
from shutil import copyfileobj

import requests
from bs4 import BeautifulSoup

from box.box_upload import get_box_client, upload_csv_safe, verify_uploaded_file
from helpers import *
from ip_addresses import ip_range
from test_filtration.filtration_test_suite import run_filtration_test_suite

ROOT_DIR = Path(__file__).resolve().parent
TEMPORARY_RAW_PATH = ROOT_DIR / "temporary_output_storage.csv"
TEMPORARY_FILTERED_PATH = ROOT_DIR / "temporary_filtered_storage.csv"

UNFILTERED_FOLDER = "EDGAR log files/Unfiltered"
FILTERED_FOLDER = "EDGAR log files/Filtered"

HEADERS = {
    "User-Agent": "Choi_Research - transactions in year 2017 - jcahn1@lion.lmu.edu",
}


def human_readable(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024


def scrape_raw_csv(link, output_path=None, headers=None):
    """
    Download a daily EDGAR zip and extract the raw CSV as-is.
    Returns (local_csv_path, source_csv_name).
    """
    response = requests.get(link, headers=headers)
    response.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("No CSV file found in zip archive")

        csv_name = csv_names[0]
        source_name = Path(csv_name).name

        if output_path is None:
            output_path = Path(source_name)
        else:
            output_path = Path(output_path)
            if output_path.suffix.lower() != ".csv":
                output_path = output_path.with_suffix(".csv")

        with archive.open(csv_name) as csv_file, open(output_path, "wb") as out_file:
            copyfileobj(csv_file, out_file)

    print(f"    saved {human_readable(output_path.stat().st_size)} -> {output_path}")
    return output_path, source_name


def filter_csv(raw_path: Path, filtered_path: Path) -> int:
    """
    Write only rows whose IP matches a target organisation (ftc / doj / doj_uncertain).
    Malformed IPs are skipped (not kept, not fatal).
    Returns the number of rows kept.
    """
    with (
        open(raw_path, "r", encoding="utf-8", newline="") as in_f,
        open(filtered_path, "w", encoding="utf-8", newline="") as out_f,
    ):
        reader = csv.DictReader(in_f)
        fieldnames = list(reader.fieldnames or []) + ["organization"]
        writer = csv.DictWriter(out_f, fieldnames=fieldnames)
        writer.writeheader()
        kept = 0
        for row in reader:
            ip = (row.get("ip") or "").strip()
            if not ip:
                continue
            org = ip_range(ip)
            if org is None:
                continue
            row["organization"] = org
            writer.writerow(row)
            kept += 1

    print(f"    filtered -> {filtered_path} ({kept} rows kept)")
    return kept


def process_filter_and_upload(link, client, output_path=TEMPORARY_RAW_PATH, headers=None):
    """
    Scrape raw CSV, filter it, validate with the filtration test suite, upload
    filtered CSV to FILTERED_FOLDER. Temp files are cleaned up on exit.

    Returns (ok, stage, reason, client). client may be refreshed after a 401.
    """
    # --- scrape ---
    try:
        local_csv_path, source_name = scrape_raw_csv(
            link, output_path=output_path, headers=headers
        )
    except Exception as error:
        reason = f"scrape: {type(error).__name__}: {error}"
        print(f"    scrape failed: {reason}")
        return False, "scrape", reason, client

    # Rename raw temp to the true source name.
    named_raw = local_csv_path.with_name(source_name)
    if named_raw != local_csv_path:
        if named_raw.exists():
            named_raw.unlink()
        local_csv_path.rename(named_raw)
        local_csv_path = named_raw

    # --- filter ---
    filtered_path = TEMPORARY_FILTERED_PATH.with_name(
        TEMPORARY_FILTERED_PATH.stem + "_" + source_name
    )
    try:
        kept = filter_csv(local_csv_path, filtered_path)
    except Exception as error:
        _cleanup(local_csv_path)
        reason = f"filter: {type(error).__name__}: {error}"
        print(f"    filter failed: {reason}")
        return False, "filter", reason, client

    # --- validate ---
    try:
        results = run_filtration_test_suite(
            original_csv_file=local_csv_path,
            filtered_csv_file=filtered_path,
        )
    except Exception as error:
        _cleanup(local_csv_path, filtered_path)
        reason = f"test: {type(error).__name__}: {error}"
        print(f"    filtration test error: {reason}")
        return False, "test", reason, client

    if not results["overall_passed"]:
        detail = (
            f"membership={results['membership_check']}, "
            f"completeness={results['completeness_check']}"
        )
        _cleanup(local_csv_path, filtered_path)
        reason = f"test: filtration validation failed: {detail}"
        print(f"    {reason}")
        return False, "test", reason, client

    print(f"    filtration tests passed (kept {kept} rows)")

    # Raw file is no longer needed.
    _cleanup(local_csv_path)

    # Rename filtered temp to the true source name so Box gets logYYYYMMDD.csv
    # in the correct year subfolder (EDGAR log files/Filtered/<year>/).
    named_filtered = filtered_path.with_name(source_name)
    if named_filtered != filtered_path:
        if named_filtered.exists():
            named_filtered.unlink()
        filtered_path.rename(named_filtered)
        filtered_path = named_filtered

    print("filepath operations complete")


    # --- upload filtered to Box ---
    print("initiating upload to Box")
    uploaded, box_reason, client = upload_csv_safe(
        filtered_path,
        client=client,
        destination_folder=FILTERED_FOLDER,
    )

    verified = False
    if uploaded is not None:
        try:
            verified, client = verify_uploaded_file(
                client,
                filtered_path,
                destination_folder=FILTERED_FOLDER,
            )
        except Exception as error:
            verified = False
            box_reason = f"box: verification failed: {type(error).__name__}: {error}"
        else:
            if not verified:
                box_reason = (
                    "box: verification failed: uploaded file was not found "
                    "in the expected Box folder"
                )

    _cleanup(filtered_path)

    if uploaded is None:
        return False, "box", box_reason or "box: unknown upload failure", client
    if not verified:
        return False, "box", box_reason or "box: upload verification failed", client

    return True, None, None, client


def _cleanup(*paths: Path) -> None:
    for p in paths:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def _advance_or_complete(task_management, year):
    """Advance current_index / year. Returns True if all years are done."""
    completion = increment()
    if completion != "complete":
        return False

    print(f"=== Year {year}: complete ===")
    next_year = str(int(year) + 1)
    task_management = load_management()
    if next_year in task_management["baseline_links"]:
        task_management["current"] = {
            "current_index": 0,
            "year": next_year,
            "links": [],
        }
        save_management(task_management)
        print(f"=== Advancing to year {next_year} ===")
        return False

    print("=== All years complete ===")
    return True


def run_normal_scrape(client):
    """Original year-by-year filtered scrape driven by async_management.json."""
    while True:
        task_management = load_management()
        current = task_management["current"]
        year = current["year"]
        test_box = bool(task_management.get("test_box", False))

        if not current.get("links"):
            print(f"=== Year {year}: fetching file list ===")
            base_url = task_management["baseline_links"][year]
            html = requests.get(base_url, headers=HEADERS).text
            parsed_html = BeautifulSoup(html, "html.parser")
            links = [elmt["href"] for elmt in parsed_html.find_all("a", href=True)]
            current["links"] = links
            current["current_index"] = 0
            save_management(task_management)
            print(f"=== Year {year}: {len(links)} files to process ===")
            continue

        links = current["links"]
        index = current["current_index"]
        link = links[index]
        file_name = link.rsplit("/", 1)[-1]
        print(f"[{year}] file {index + 1}/{len(links)}: {file_name}")

        ok, stage, reason, client = process_filter_and_upload(
            link, client, headers=HEADERS
        )
        if not ok:
            log_failed_file(link, reason)
            if stage == "box" and test_box:
                print("    quitting due to Box failure (test_box=true)")
                break
            print(f"    logged failure ({stage}); continuing")
            if _advance_or_complete(task_management, year):
                break
            continue

        if _advance_or_complete(task_management, year):
            break


if __name__ == "__main__":
    client = get_box_client()
    run_normal_scrape(client)
