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
LINKS_FILE_TEMPLATE = ROOT_DIR / "links_{year}.txt"
LINKS_2017_FILE = ROOT_DIR / "links_2017.txt"
LINKS_2017_INDEX = ROOT_DIR / "links_2017.index"


def _is_leap_year(y: int) -> bool:
    return (y % 4 == 0) and (y % 100 != 0 or y % 400 == 0)


def generate_year_links(year: int) -> list[str]:
    """Generate expected daily EDGAR log ZIP URLs for a full calendar year.

    Writes a `links_<year>.txt` file with one URL per line and returns the list.
    """
    month_days = [31, 28 + (1 if _is_leap_year(year) else 0), 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    links: list[str] = []
    for month in range(1, 13):
        days = month_days[month - 1]
        qtr = ((month - 1) // 3) + 1
        for day in range(1, days + 1):
            y = str(year)
            mm = f"{month:02d}"
            dd = f"{day:02d}"
            yyyy = f"{year}{mm}{dd}"
            link = f"http://www.sec.gov/dera/data/Public-EDGAR-log-file-data/{y}/Qtr{qtr}/log{yyyy}.zip"
            links.append(link)

    # write the file so future runs use the static list
    path = ROOT_DIR / f"links_{year}.txt"
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(links) + "\n")

    return links

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
            # 2017-only special-case: load from the static links file and resume via index.
            if str(year) == "2017" and LINKS_2017_FILE.exists():
                print(f"=== Year {year}: loading links from {LINKS_2017_FILE.name} ===")
                with open(LINKS_2017_FILE, "r", encoding="utf-8") as f:
                    links = [line.strip() for line in f.readlines() if line.strip()]

                # If an index file exists, use it to resume progress.
                start_index = 0
                if LINKS_2017_INDEX.exists():
                    try:
                        with open(LINKS_2017_INDEX, "r", encoding="utf-8") as idxf:
                            start_index = int(idxf.read().strip() or "0")
                    except Exception:
                        start_index = 0

                current["links"] = links
                current["current_index"] = start_index
                # For 2017-only, initialize a day_count (DOY) starting at 90
                # so the first processed link will correspond to day 91 (Apr 1).
                if str(year) == "2017":
                    current.setdefault("day_count", 90)

                save_management(task_management)
                print(f"=== Year {year}: {len(links)} files to process (starting at {start_index}) ===")
                continue

            # Fallback: fetch file list from baseline URL in async_management.json
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

        # On success: if year is 2017, use day_count/indexing logic until 365 days complete.
        if str(year) == "2017":
            # increment day_count and move to next index
            current_index = int(current.get("current_index", 0))
            next_index = current_index + 1
            current["current_index"] = next_index
            current["day_count"] = int(current.get("day_count", 90)) + 1

            # persist per-year index file for resuming
            try:
                with open(LINKS_2017_INDEX, "w", encoding="utf-8") as idxf:
                    idxf.write(str(next_index))
            except Exception:
                pass

            save_management(task_management)
            print(f"    2017 progress: day {current['day_count']} (index {next_index})")

            # If we've reached or passed day 365, advance year and stop this pass
            if current["day_count"] >= 365:
                print(f"=== Year {year}: reached {current['day_count']} days; completing year ===")
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
                    break

                print("=== All years complete ===")
                break

            # Continue processing next link in 2017
            continue

        # Non-2017 default advancement
        if _advance_or_complete(task_management, year):
            break


def run_2017_scrape(client):
    #on file 45

    """Handle 2017 scraping using links_2017.txt and links_2017.index with day_count."""
    task_management = load_management()
    current = task_management["current"]
    year = current["year"]

    print(f"=== Starting 2017-specific scrape (year={year}) ===")

    # Load static links file
    if not LINKS_2017_FILE.exists():
        print(f"links_2017.txt not found; falling back to normal flow")
        run_normal_scrape(client)
        return

    with open(LINKS_2017_FILE, "r", encoding="utf-8") as f:
        links = [line.strip() for line in f.readlines() if line.strip()]

    start_index = 0
    if LINKS_2017_INDEX.exists():
        try:
            with open(LINKS_2017_INDEX, "r", encoding="utf-8") as idxf:
                start_index = int(idxf.read().strip() or "0")
        except Exception:
            start_index = int(current.get("current_index", 0) or 0)

    day_count = int(current.get("day_count", 0))

    total = len(links)
    print(f"=== 2017: {total} links loaded, starting at index {start_index}")

    index = start_index
    while index < total:
        link = links[index]
        file_name = link.rsplit("/", 1)[-1]
        print(f"[2017] file {index + 1}/{total}: {file_name} (day {day_count + 1})")

        ok, stage, reason, client = process_and_upload(link, client, headers=HEADERS)
        if not ok:
            log_failed_file(link, reason)
            print(f"    logged failure ({stage}): {reason}")
        else:
            print("    success")

        # advance index and day_count regardless of success to count attempted days
        index += 1
        day_count += 1

        # persist progress to management and index file
        current["links"] = links
        current["current_index"] = index
        current["day_count"] = day_count
        task_management["current"] = current
        save_management(task_management)

        try:
            with open(LINKS_2017_INDEX, "w", encoding="utf-8") as idxf:
                idxf.write(str(index))
        except Exception:
            pass

        if day_count >= 275:
            print(f"=== Year 2017: reached day_count {day_count}; completing 2017 run ===")
            # Advance year in management
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
            else:
                print("=== All years complete ===")
            break

        # Auxiliary printing: every 10 files, show compact progress
        if index % 10 == 0:
            print(f"--- 2017 progress: processed {index}/{total} files; day {day_count} ---")

    print("=== 2017-specific scrape finished ===")


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
        run_2017_scrape(client)
