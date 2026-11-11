"""
Filtered scrape for 2017 only — resumes from progress.json (starts 2017-04-01).

Downloads EDGAR, filters FTC/DOJ IPs, validates, uploads to
EDGAR log files/Filtered/2017/. Failures are appended to progress.json.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

TEST_DIR = Path(__file__).resolve().parent
ROOT_DIR = TEST_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from box.box_upload import get_box_client
from check_filtered_failure import sec_zip_url
from webscraper_filtered import HEADERS, process_filter_and_upload

PROGRESS_PATH = TEST_DIR / "progress.json"


def load_progress() -> dict:
    with open(PROGRESS_PATH, "r", encoding="utf-8") as file:
        return json.load(file)


def save_progress(data: dict) -> None:
    PROGRESS_PATH.write_text(
        json.dumps(data, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def log_failure(progress: dict, day: date, link: str, stage: str | None, reason: str) -> None:
    # Record scrape / filter / test / Box failures with date, link, and reason.
    progress.setdefault("failed_files", []).append(
        {
            "date": day.isoformat(),
            "filename": f"log{day.year}{day.month:02d}{day.day:02d}.csv",
            "link": link,
            "stage": stage,
            "reason": reason,
        }
    )
    save_progress(progress)


def advance_current_date(progress: dict, day: date, end: date) -> date | None:
    # Move resume pointer to the next calendar day (or clear when finished).
    nxt = day + timedelta(days=1)
    if nxt > end:
        progress["current_date"] = None
        save_progress(progress)
        return None
    progress["current_date"] = nxt.isoformat()
    save_progress(progress)
    return nxt


def run() -> None:
    progress = load_progress()
    raw_current = progress.get("current_date")
    if not raw_current:
        print("=== 2017 filtered scrape already complete ===")
        return

    current = date.fromisoformat(raw_current)
    end = date.fromisoformat(progress.get("end_date", "2017-12-31"))
    if current < date(2017, 4, 1):
        current = date(2017, 4, 1)
        progress["current_date"] = current.isoformat()
        save_progress(progress)

    client = get_box_client()
    print(
        f"=== 2017 filtered scrape from {current.isoformat()} "
        f"through {end.isoformat()} ==="
    )

    day = current
    while day <= end:
        link = sec_zip_url(day.year, day.month, day.day)
        filename = f"log{day.year}{day.month:02d}{day.day:02d}.zip"
        print(f"[2017] {day.isoformat()}: {filename}")

        # Scrape → filter → filtration tests → upload to Filtered/<year>/.
        ok, stage, reason, client = process_filter_and_upload(
            link, client, headers=HEADERS
        )
        if not ok:
            log_failure(
                progress,
                day,
                link,
                stage,
                reason or f"{stage}: unknown failure",
            )
            print(f"    logged failure ({stage}); continuing")
        else:
            print("    success")

        # Always advance so a rerun resumes after this day.
        nxt = advance_current_date(progress, day, end)
        if nxt is None:
            print("=== 2017 filtered scrape complete ===")
            return
        day = nxt
        progress = load_progress()

    print("=== 2017 filtered scrape complete ===")


if __name__ == "__main__":
    run()
