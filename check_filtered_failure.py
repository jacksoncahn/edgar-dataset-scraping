"""
List EDGAR log files/Filtered/<year> on Box, find missing daily CSVs,
and write fully downloadable SEC zip URLs to missing_filtered_days.json.
"""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta
from pathlib import Path

from box.box_upload import _folder_id_for_path, get_box_client

ROOT_DIR = Path(__file__).resolve().parent
FILTERED_BASE = "EDGAR log files/Filtered"
OUTPUT_JSON = ROOT_DIR / "missing_filtered_days.json"

LOG_NAME_RE = re.compile(r"^log(\d{4})(\d{2})(\d{2})\.csv$", re.IGNORECASE)
YEAR_FOLDER_RE = re.compile(r"^\d{4}$")

SEC_ZIP_TEMPLATE = (
    "https://www.sec.gov/dera/data/Public-EDGAR-log-file-data/"
    "{year}/Qtr{quarter}/log{year}{month:02d}{day:02d}.zip"
)


def quarter_for_month(month: int) -> int:
    if month <= 3:
        return 1
    if month <= 6:
        return 2
    if month <= 9:
        return 3
    return 4


def sec_zip_url(year: int, month: int, day: int) -> str:
    link = SEC_ZIP_TEMPLATE.format(
        year=year,
        quarter=quarter_for_month(month),
        month=month,
        day=day,
    )
    return link


def all_dates_in_year(year: int) -> list[date]:
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    days = (end - start).days + 1
    return [start + timedelta(days=i) for i in range(days)]


def list_year_folders(client) -> list[str]:
    folder_id = _folder_id_for_path(client, FILTERED_BASE)
    years: list[str] = []
    for item in client.folder(folder_id).get_items():
        if item.type == "folder" and YEAR_FOLDER_RE.match(item.name):
            years.append(item.name)
    return sorted(years)


def present_dates_in_year_folder(client, year: str) -> set[date]:
    folder_id = _folder_id_for_path(client, f"{FILTERED_BASE}/{year}")
    present: set[date] = set()
    for item in client.folder(folder_id).get_items():
        if item.type != "file":
            continue
        match = LOG_NAME_RE.match(item.name)
        if not match:
            continue
        y, m, d = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if y != int(year):
            continue
        try:
            present.add(date(y, m, d))
        except ValueError:
            continue
    return present


def missing_links_for_year(client, year: str) -> list[str]:
    year_int = int(year)
    present = present_dates_in_year_folder(client, year)
    missing: list[str] = []
    dates = all_dates_in_year(year_int)
    if year == 2017:
        #dates from jan 1 to march 31
        dates = [date(2017, 1, 1) + timedelta(days=i) for i in range(90)]
    for day in dates:
        if day not in present:
            missing.append(sec_zip_url(day.year, day.month, day.day))
    print(f"Missing {missing}")
    return missing


def main() -> None:
    client = get_box_client()
    years = list_year_folders(client)
    if not years:
        print(f"No year folders found under '{FILTERED_BASE}'.")
        OUTPUT_JSON.write_text("{}\n", encoding="utf-8")
        print(f"Wrote empty map -> {OUTPUT_JSON}")
        return

    result: dict[str, list[str]] = {}
    # for year in years:
    #     print(f"Checking {FILTERED_BASE}/{year} ...")
    #     links = missing_links_for_year(client, year)
    #     result[year] = links
    #     print(f"  missing {len(links)} / {366 if calendar.isleap(int(year)) else 365} days")

    print(f"Checking {FILTERED_BASE}/{2017} ...")
    links = missing_links_for_year(client, 2017)
    result[2017] = links
    print(f"  missing {len(links)} / 90 days")


    OUTPUT_JSON.write_text(
        json.dumps(result, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote -> {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
