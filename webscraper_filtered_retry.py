"""
Retry working filtered EDGAR days from retry_working_links.json.

Always starts at the beginning of that file. Reuses scrape/filter/upload from
webscraper_filtered.py.
"""

from __future__ import annotations

import json
from pathlib import Path

from box.box_upload import get_box_client
from helpers import load_management, log_failed_file
from webscraper_filtered import HEADERS, process_filter_and_upload

ROOT_DIR = Path(__file__).resolve().parent
RETRY_WORKING_LINKS_JSON = ROOT_DIR / "retry_working_links.json"


def load_retry_working_links(
    path: Path = RETRY_WORKING_LINKS_JSON,
) -> list[tuple[str, str]]:
    """
    Load retry_working_links.json as [(year, link), ...] from the start.
    Skips empty year arrays, non-year keys, and blank link strings.
    """
    if not path.exists():
        raise FileNotFoundError(f"Retry working link file not found: {path}")

    with open(path, "r", encoding="utf-8") as file:
        data = json.load(file)

    jobs: list[tuple[str, str]] = []
    for year in sorted(data.keys()):
        if not str(year).isdigit():
            continue
        links = data.get(year) or []
        if not isinstance(links, list):
            continue
        for link in links:
            link = str(link).strip()
            if link:
                jobs.append((str(year), link))
    return jobs


def run_retry_working_links(client):
    """Retry all links in retry_working_links.json into Filtered/<year>/."""
    jobs = load_retry_working_links()
    if not jobs:
        print(f"No links in {RETRY_WORKING_LINKS_JSON}")
        return

    test_box = bool(load_management().get("test_box", False))
    total = len(jobs)
    print(
        f"=== Retrying {total} working link(s) from start of "
        f"{RETRY_WORKING_LINKS_JSON.name} ==="
    )

    for index, (year, link) in enumerate(jobs, start=1):
        file_name = link.rsplit("/", 1)[-1]
        print(f"[{year}] file {index}/{total}: {file_name}")

        ok, stage, reason, client = process_filter_and_upload(
            link, client, headers=HEADERS
        )
        if ok:
            print("    success")
            continue

        log_failed_file(link, reason)
        if stage == "box" and test_box:
            print("    quitting due to Box failure (test_box=true)")
            break
        print(f"    logged failure ({stage}); continuing")

    print("=== Working-links retry pass complete ===")


if __name__ == "__main__":
    client = get_box_client()
    run_retry_working_links(client)
