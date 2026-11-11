import io
import json
import re
import sys
import time
import zipfile
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.exceptions import ChunkedEncodingError, ConnectionError, RequestException, Timeout

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from helpers import load_management, save_management

SIZE_DIR = Path(__file__).resolve().parent
SIZE_MANAGEMENT_FILE = SIZE_DIR / "async_management.json"
SECTIONED_SIZES_FILE = SIZE_DIR / "sectioned_sizes.json"

HEADERS = {
    "User-Agent": "Choi_Research - jcahn1@lion.lmu.edu",
}

DOWNLOAD_TIMEOUT_SECONDS = 120
DOWNLOAD_MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2

RETRYABLE_REQUEST_ERRORS = (
    ChunkedEncodingError,
    ConnectionError,
    Timeout,
)

def human_readable_mb_gb(num_bytes):
    mb_size = num_bytes / (1024**2)
    if mb_size >= 1024:
        gb_size = mb_size / 1024
        return f"{gb_size:.1f} GB"
    return f"{mb_size:.1f} MB"


def parse_date_from_link(link):
    match = re.search(r"log(\d{4})(\d{2})(\d{2})\.zip$", link)
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def build_empty_sectioned_sizes():
    management = load_management(SIZE_MANAGEMENT_FILE)
    years = sorted(management["baseline_links"].keys())
    return {
        "years": {
            year: {
                "files": [],
                "year_total_bytes": 0,
                "year_total_human": "0.0 MB",
            }
            for year in years
        },
        "total_size_bytes": 0,
        "total_size_human": "0.0 MB",
    }


def load_sectioned_sizes():
    if not SECTIONED_SIZES_FILE.exists():
        data = build_empty_sectioned_sizes()
        save_sectioned_sizes(data)
        return data

    with open(SECTIONED_SIZES_FILE, "r", encoding="utf-8") as file:
        contents = file.read().strip()
        if not contents:
            data = build_empty_sectioned_sizes()
            save_sectioned_sizes(data)
            return data
        return json.loads(contents)


def save_sectioned_sizes(data):
    with open(SECTIONED_SIZES_FILE, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def record_failed_link(link, status, status_text):
    data = load_management(SIZE_MANAGEMENT_FILE)
    data.setdefault("failed_links", []).append(
        {
            "link": link,
            "status": status,
            "status_text": status_text,
        }
    )
    save_management(data, SIZE_MANAGEMENT_FILE)


def record_failed_year(year, link, status, status_text):
    data = load_management(SIZE_MANAGEMENT_FILE)
    data.setdefault("failed_years", []).append(
        {
            "year": year,
            "link": link,
            "status": status,
            "status_text": status_text,
        }
    )
    save_management(data, SIZE_MANAGEMENT_FILE)


def log_failure(link, status, status_text):
    print(f"    failed: {status_text} -- logged to failed_links")
    record_failed_link(link, status, status_text)


def log_year_failure(year, link, status, status_text):
    print(f"=== Year {year}: {status_text} -- logged to failed_years")
    record_failed_year(year, link, status, status_text)


def fetch_with_retries(url, headers=None, max_retries=DOWNLOAD_MAX_RETRIES, timeout=DOWNLOAD_TIMEOUT_SECONDS):
    headers = headers or HEADERS
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            return requests.get(url, headers=headers, timeout=timeout)
        except RETRYABLE_REQUEST_ERRORS as error:
            last_error = error
            if attempt < max_retries:
                wait_seconds = attempt * RETRY_BACKOFF_SECONDS
                print(
                    f"    download error (attempt {attempt}/{max_retries}): "
                    f"{type(error).__name__} -- retrying in {wait_seconds}s"
                )
                time.sleep(wait_seconds)
            else:
                raise last_error from error

    raise RuntimeError(f"Failed to download {url}")


def record_size_entry(year, link, size_bytes):
    data = load_sectioned_sizes()
    year_bucket = data["years"].setdefault(
        year,
        {
            "files": [],
            "year_total_bytes": 0,
            "year_total_human": "0.0 MB",
        },
    )

    size_human = human_readable_mb_gb(size_bytes)
    year_bucket["files"].append(
        {
            "link": link,
            "date": parse_date_from_link(link),
            "size_bytes": size_bytes,
            "size_human": size_human,
        }
    )
    year_bucket["year_total_bytes"] += size_bytes
    year_bucket["year_total_human"] = human_readable_mb_gb(year_bucket["year_total_bytes"])

    data["total_size_bytes"] += size_bytes
    data["total_size_human"] = human_readable_mb_gb(data["total_size_bytes"])

    save_sectioned_sizes(data)
    return year_bucket["year_total_human"], data["total_size_human"]


def increment_size_task():
    data = load_management(SIZE_MANAGEMENT_FILE)
    links = data["current"].get("links", [])
    current_index = int(data["current"].get("current_index", 0))

    if current_index >= len(links) - 1:
        return "complete"

    data["current"]["current_index"] = current_index + 1
    save_management(data, SIZE_MANAGEMENT_FILE)
    return "incomplete"


def record_size(link, output_path=None, headers=None):
    headers = headers or HEADERS

    try:
        response = fetch_with_retries(link, headers=headers)
    except RequestException as error:
        log_failure(link, 0, f"{type(error).__name__}: {error}")
        return None

    if response.status_code != 200:
        log_failure(link, response.status_code, response.reason)
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            csv_names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
            if not csv_names:
                log_failure(link, 0, "ValueError: No CSV file found in zip archive")
                return None

            csv_name = csv_names[0]
            csv_size_bytes = archive.getinfo(csv_name).file_size
    except zipfile.BadZipFile as error:
        log_failure(link, 0, f"BadZipFile: {error}")
        return None
    except (KeyError, ValueError) as error:
        log_failure(link, 0, f"{type(error).__name__}: {error}")
        return None

    print(f"    raw csv size: {human_readable_mb_gb(csv_size_bytes)}")
    return csv_size_bytes


if __name__ == "__main__":
    while True:
        task_management = load_management(SIZE_MANAGEMENT_FILE)
        current = task_management["current"]
        year = current["year"]

        if not current.get("links"):
            print(f"=== Year {year}: fetching file list ===")
            base_url = task_management["baseline_links"][year]
            try:
                response = fetch_with_retries(base_url, headers=HEADERS)
            except RequestException as error:
                status_text = f"{type(error).__name__}: {error}"
                log_year_failure(year, base_url, 0, status_text)
                time.sleep(DOWNLOAD_MAX_RETRIES * RETRY_BACKOFF_SECONDS)
                continue

            if response.status_code != 200:
                status_text = f"{response.status_code} {response.reason}"
                log_year_failure(year, base_url, response.status_code, status_text)
                time.sleep(DOWNLOAD_MAX_RETRIES * RETRY_BACKOFF_SECONDS)
                continue

            parsed_html = BeautifulSoup(response.text, "html.parser")
            links = [elmt["href"] for elmt in parsed_html.find_all("a", href=True)]
            current["links"] = links
            current["current_index"] = 0
            save_management(task_management, SIZE_MANAGEMENT_FILE)
            print(f"=== Year {year}: {len(links)} files to process ===")
            continue

        links = current["links"]
        index = current["current_index"]
        link = links[index]
        file_name = link.rsplit("/", 1)[-1]
        print(f"[{year}] file {index + 1}/{len(links)}: {file_name}")

        file_size_bytes = record_size(link, headers=HEADERS)
        if file_size_bytes is not None:
            year_total_human, total_human = record_size_entry(year, link, file_size_bytes)
            print(
                f"    file size {human_readable_mb_gb(file_size_bytes)}  "
                f"(year total: {year_total_human}, overall: {total_human})"
            )

        completion = increment_size_task()
        if completion == "complete":
            year_data = load_sectioned_sizes()["years"].get(year, {})
            year_total = year_data.get("year_total_human", "0.0 MB")
            print(f"=== Year {year}: complete ({year_total}) ===")
            next_year = str(int(year) + 1)
            if next_year in task_management["baseline_links"]:
                task_management["current"] = {
                    "current_index": 0,
                    "year": next_year,
                    "links": [],
                }
                save_management(task_management, SIZE_MANAGEMENT_FILE)
                print(f"=== Advancing to year {next_year} ===")
            else:
                total_human = load_sectioned_sizes().get("total_size_human", "0.0 MB")
                print(f"=== All years complete ({total_human}) ===")
                break
