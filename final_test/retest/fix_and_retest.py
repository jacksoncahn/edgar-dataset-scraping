"""
Retry / fix days that failed in final_testing_failures.json.

- edgar:*  -> re-run the Box-vs-EDGAR test only
- box:* (and other data failures) -> rescrape + upload Unfiltered, then filter +
  upload Filtered, then re-run the test

Progress: retest_progress.json
New failures: retest_failures.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

RETEST_DIR = Path(__file__).resolve().parent
FINAL_TEST_DIR = RETEST_DIR.parent
ROOT_DIR = FINAL_TEST_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(FINAL_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(FINAL_TEST_DIR))

from box.box_auth import ensure_box_auth
from box.box_upload import get_box_client
from check_filtered_failure import sec_zip_url
from webscraper_filtered import process_filter_and_upload
from webscraper_unfiltered import HEADERS, process_and_upload

import final_test_suite as fts

SOURCE_FAILURES = FINAL_TEST_DIR / "final_testing_failures.json"
PROGRESS_PATH = RETEST_DIR / "retest_progress.json"
RETEST_FAILURES_PATH = RETEST_DIR / "retest_failures.json"

# Keep retest temps inside this folder so they do not clash with final_test temps.
TEMP_EDGAR = RETEST_DIR / "temp_edgar.csv"
TEMP_UNFILTERED = RETEST_DIR / "temp_box_unfiltered.csv"
TEMP_FILTERED = RETEST_DIR / "temp_box_filtered.csv"


# --- simple progress load/save (not the full helpers.async_management shape) ---

def load_progress(path: Path = PROGRESS_PATH) -> dict:
    if not path.exists():
        data = {"current_index": 0, "links": []}
        save_progress(data, path)
        return data
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        data = {"current_index": 0, "links": []}
        save_progress(data, path)
        return data
    data = json.loads(raw)
    data.setdefault("current_index", 0)
    data.setdefault("links", [])
    return data


def save_progress(data: dict, path: Path = PROGRESS_PATH) -> None:
    path.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def log_retest_failure(year: str, filename: str, reason: str) -> None:
    """Same shape as final_testing_failures.json entries."""
    if RETEST_FAILURES_PATH.exists():
        data = json.loads(RETEST_FAILURES_PATH.read_text(encoding="utf-8") or "{}")
    else:
        data = {"failures": {}}

    failures = data.setdefault("failures", {})
    year_list = failures.setdefault(str(year), [])
    year_list.append({"filename": filename, "reason": reason})
    RETEST_FAILURES_PATH.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"    logged retest failure -> {RETEST_FAILURES_PATH.name}: {filename}")


def classify_action(reason: str) -> str:
    """
    box:*   -> rebuild (full unfiltered + filtered upload), then retest
    edgar:* -> retest only
    other   -> rebuild then retest (bad Box data / match failures)
    """
    text = (reason or "").strip().lower()
    if text.startswith("box:"):
        return "rebuild"
    if text.startswith("edgar:"):
        return "retest"
    return "rebuild"


def build_jobs_from_source_failures(
    path: Path = SOURCE_FAILURES,
) -> list[dict]:
    """
    Flatten final_testing_failures.json into jobs.
    One job per filename; if multiple reasons exist, rebuild wins over retest.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    by_file: dict[str, dict] = {}

    for year, items in (data.get("failures") or {}).items():
        for item in items or []:
            filename = str(item.get("filename") or "").strip()
            reason = str(item.get("reason") or "").strip()
            if not filename:
                continue
            log_date = fts.log_date_from_name(filename)
            if log_date is None:
                continue
            link = sec_zip_url(log_date.year, log_date.month, log_date.day)
            action = classify_action(reason)
            key = filename.lower()
            existing = by_file.get(key)
            if existing is None or (
                existing["action"] == "retest" and action == "rebuild"
            ):
                by_file[key] = {
                    "year": str(year),
                    "filename": filename,
                    "link": link,
                    "reason": reason,
                    "action": action,
                }

    # Stable chronological order for resume.
    jobs = sorted(
        by_file.values(),
        key=lambda job: fts.log_date_from_name(job["filename"]) or job["filename"],
    )
    return jobs


def ensure_progress_links(progress: dict) -> dict:
    # First run: fill the links queue from the original failure log.
    if progress.get("links"):
        return progress
    jobs = build_jobs_from_source_failures()
    progress["links"] = jobs
    progress["current_index"] = 0
    save_progress(progress)
    print(f"=== Loaded {len(jobs)} unique failure job(s) from {SOURCE_FAILURES.name} ===")
    return progress


def advance_progress(progress: dict) -> bool:
    """Advance index. Returns True when the queue is finished."""
    links = progress.get("links") or []
    index = int(progress.get("current_index", 0))
    if index >= len(links) - 1:
        progress["current_index"] = len(links)
        save_progress(progress)
        return True
    progress["current_index"] = index + 1
    save_progress(progress)
    return False


def retest_one_day(link: str, year: str, filename: str, client):
    """
    Mirror final_test_suite.process_one_day, but log into retest_failures.json
    and use temps under final_test/retest/.
    """
    sec_link = fts.normalize_sec_zip_url(link)
    print(f"    filename={filename}")
    print(f"    sec_link={sec_link}")

    # --- EDGAR download ---
    try:
        fts.scrape_edgar_csv(sec_link, TEMP_EDGAR)
    except Exception as error:
        # Include HTTP status when available (same idea as final_test_suite).
        import requests

        if isinstance(error, requests.HTTPError):
            status = getattr(error.response, "status_code", "?")
            log_retest_failure(year, filename, f"edgar: HTTP {status} for {sec_link}")
        else:
            log_retest_failure(
                year, filename, f"edgar: {type(error).__name__}: {error}"
            )
        fts._cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    # --- Box Unfiltered download ---
    unfiltered_path, box_reason, client = fts.download_csv_safe(
        filename,
        client=client,
        destination_folder=fts.UNFILTERED_FOLDER,
        local_path=TEMP_UNFILTERED,
    )
    if unfiltered_path is None:
        log_retest_failure(
            year, filename, box_reason or "box: unfiltered download failed"
        )
        fts._cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    # --- Box Filtered download ---
    filtered_path, box_reason, client = fts.download_csv_safe(
        filename,
        client=client,
        destination_folder=fts.FILTERED_FOLDER,
        local_path=TEMP_FILTERED,
    )
    if filtered_path is None:
        log_retest_failure(
            year, filename, box_reason or "box: filtered download failed"
        )
        fts._cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client

    # --- Unfiltered row/column equality ---
    unfiltered_reason = fts.compare_unfiltered_row_by_row(TEMP_EDGAR, TEMP_UNFILTERED)
    if unfiltered_reason is not None:
        log_retest_failure(year, filename, unfiltered_reason)
        fts._cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
        return client
    print("    unfiltered compare: passed")

    # --- Filtered membership + completeness ---
    for reason in fts.run_filtered_membership_completeness(
        TEMP_UNFILTERED, TEMP_FILTERED
    ):
        log_retest_failure(year, filename, reason)

    # --- Organization label check ---
    for reason in fts.run_organization_categorization_check(TEMP_FILTERED):
        log_retest_failure(year, filename, reason)

    print("    filtered checks: finished")
    fts._cleanup(TEMP_EDGAR, TEMP_UNFILTERED, TEMP_FILTERED)
    return client


def rebuild_day(link: str, client):
    """
    Full repair for box (and similar) failures:
    1) scrape + upload Unfiltered
    2) scrape + filter + upload Filtered
    Returns (ok, stage, reason, client).
    """
    sec_link = fts.normalize_sec_zip_url(link)

    print("    step 1/2: scrape + upload Unfiltered")
    ok, stage, reason, client = process_and_upload(
        sec_link, client, headers=HEADERS
    )
    if not ok:
        return False, stage or "unfiltered", reason, client
    print("    step 1/2: Unfiltered upload ok")

    print("    step 2/2: scrape + filter + upload Filtered")
    ok, stage, reason, client = process_filter_and_upload(
        sec_link, client, headers=HEADERS
    )
    if not ok:
        return False, stage or "filtered", reason, client
    print("    step 2/2: Filtered upload ok")

    return True, None, None, client


def process_job(job: dict, client):
    """
    Box failures: full rebuild, then retest immediately.
    Edgar failures: retest only.
    """
    year = job["year"]
    filename = job["filename"]
    link = job["link"]
    action = job.get("action", "retest")
    original = job.get("reason") or ""
    print(f"    original_reason: {original[:120]}")
    print(f"    action: {action}")

    # Box (and other rebuild) path: entire pipeline first...
    if action == "rebuild":
        print("    box/data failure -> full rebuild, then immediate retest")
        ok, stage, reason, client = rebuild_day(link, client)
        if not ok:
            log_retest_failure(
                year,
                filename,
                f"rebuild-{stage}: {reason or 'unknown failure'}",
            )
            print(f"    rebuild failed ({stage}); skipping retest")
            return client
        print("    rebuild finished -> starting retest now")

    # ...then always retest right after a successful rebuild (or edgar-only).
    print("    retesting against Box")
    client = retest_one_day(link, year, filename, client)
    return client


def run() -> None:
    # Auth first so a separate `python box/box_auth.py` run is not required.
    print("=== Ensuring Box auth ===")
    ensure_box_auth()

    progress = ensure_progress_links(load_progress())
    links = progress.get("links") or []
    if not links:
        print(f"No jobs found in {SOURCE_FAILURES}")
        return

    index = int(progress.get("current_index", 0))
    if index >= len(links):
        print("=== Retest queue already complete ===")
        return

    client = get_box_client()
    print(
        f"=== Fix-and-retest from index {index + 1}/{len(links)} "
        f"({PROGRESS_PATH.name}) ==="
    )

    while True:
        progress = load_progress()
        links = progress.get("links") or []
        index = int(progress.get("current_index", 0))
        if index >= len(links):
            print("=== Fix-and-retest complete ===")
            break

        job = links[index]
        print(
            f"[{job.get('year')}] job {index + 1}/{len(links)}: "
            f"{job.get('filename')} ({job.get('action')})"
        )
        client = process_job(job, client)

        if advance_progress(progress):
            print("=== Fix-and-retest complete ===")
            break


if __name__ == "__main__":
    run()
