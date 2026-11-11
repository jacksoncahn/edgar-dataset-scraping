import io
import json
import zipfile
from pathlib import Path
from shutil import copyfileobj

import requests
from bs4 import BeautifulSoup

from box.box_upload import get_box_client, upload_csv_safe
from helpers import *

ROOT_DIR = Path(__file__).resolve().parent
TEMPORARY_OUTPUT_PATH = ROOT_DIR / "temporary_output_storage.csv"
MISSING_FILTERED_JSON = ROOT_DIR / "missing_filtered_days.json"

# True  -> retry all links from missing_filtered_days.json (from the start)
# False -> normal year-by-year scrape via async_management.json
RETRY_MISSING = False

UNFILTERED_FOLDER = "EDGAR log files/Unfiltered"

HEADERS = {
    "User-Agent": "Choi_Research - jcahn1@lion.lmu.edu",
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


def process_and_upload(link, client, output_path=TEMPORARY_OUTPUT_PATH, headers=None):
    """
    Download raw CSV, upload to Box, then remove the local temp file.

    Returns (ok, stage, reason, client):
      - ok True on full success
      - stage "scrape" or "box" on failure, with a reason string
      - client may be refreshed after a 401
    """
    try:
        local_csv_path, source_name = scrape_raw_csv(
            link, output_path=output_path, headers=headers
        )
    except Exception as error:
        reason = f"scrape: {type(error).__name__}: {error}"
        print(f"    scrape failed: {reason}")
        return False, "scrape", reason, client

    # Rename temp file to the original CSV name so Box gets a meaningful filename.
    named_path = local_csv_path.with_name(source_name)
    if named_path != local_csv_path:
        if named_path.exists():
            named_path.unlink()
        local_csv_path.rename(named_path)
        local_csv_path = named_path

    uploaded, box_reason, client = upload_csv_safe(
        local_csv_path,
        client=client,
        destination_folder=UNFILTERED_FOLDER,
    )

    if local_csv_path.exists():
        local_csv_path.unlink()

    if uploaded is None:
        return False, "box", box_reason or "box: unknown upload failure", client

    return True, None, None, client


def load_missing_links(path: Path = MISSING_FILTERED_JSON) -> list[tuple[str, str]]:
    """
    Load missing_filtered_days.json as [(year, link), ...] from the start.
    Skips empty year arrays and blank link strings. No start-date filter.
    """
    if not path.exists():
        raise FileNotFoundError(
            f"Missing link file not found: {path}. "
            "Run check_filtered_failure.py first."
        )

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    jobs: list[tuple[str, str]] = []
    for year in sorted(data.keys()):
        links = data.get(year) or []
        if not isinstance(links, list):
            continue
        for link in links:
            link = str(link).strip()
            if link:
                jobs.append((str(year), link))
    return jobs


def _advance_or_complete(task_management, year):
    """Advance current_index / year. Returns True if all years are done."""
    completion = increment()
    if completion != "complete":
        return False

    print(f"=== Year {year}: complete ===")
    next_year = str(int(year) + 1)
    # Reload so failed_files / other writes are not overwritten.
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
    """Year-by-year unfiltered scrape driven by async_management.json."""
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

        ok, stage, reason, client = process_and_upload(link, client, headers=HEADERS)
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


def run_missing_retry(client):
    """Retry all gaps from missing_filtered_days.json into Unfiltered/<year>/."""
    jobs = load_missing_links()
    if not jobs:
        print(f"No missing links in {MISSING_FILTERED_JSON}")
        return

    test_box = bool(load_management().get("test_box", False))
    total = len(jobs)
    print(
        f"=== Retrying {total} missing file(s) from start of "
        f"{MISSING_FILTERED_JSON.name} ==="
    )

    for index, (year, link) in enumerate(jobs, start=1):
        file_name = link.rsplit("/", 1)[-1]
        print(f"[{year}] file {index}/{total}: {file_name}")

        ok, stage, reason, client = process_and_upload(link, client, headers=HEADERS)
        if ok:
            print("    success")
            continue

        log_failed_file(link, reason)
        if stage == "box" and test_box:
            print("    quitting due to Box failure (test_box=true)")
            break
        print(f"    logged failure ({stage}); continuing")

    print("=== Missing-file retry pass complete ===")


if __name__ == "__main__":
    client = get_box_client()
    if RETRY_MISSING:
        run_missing_retry(client)
    else:
        run_normal_scrape(client)
