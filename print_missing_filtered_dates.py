"""
Print missing EDGAR log dates under EDGAR log files/Filtered/<year>.

For each year folder, list missing calendar days grouped by month,
showing both the date and the expected CSV filename.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date

from box.box_upload import get_box_client
from check_filtered_failure import (
    FILTERED_BASE,
    all_dates_in_year,
    list_year_folders,
    present_dates_in_year_folder,
)


def csv_name_for(day: date) -> str:
    return f"log{day.year}{day.month:02d}{day.day:02d}.csv"


def missing_dates_for_year(client, year: str) -> list[date]:
    present = present_dates_in_year_folder(client, year)
    return [day for day in all_dates_in_year(int(year)) if day not in present]


def print_year_missing(year: str, missing: list[date]) -> None:
    total_days = 366 if calendar.isleap(int(year)) else 365
    print(f"\n=== {FILTERED_BASE}/{year} ===")
    print(f"missing {len(missing)} / {total_days} days")

    by_month: dict[int, list[date]] = defaultdict(list)
    for day in missing:
        by_month[day.month].append(day)

    for month in range(1, 13):
        month_days = by_month.get(month, [])
        label = calendar.month_name[month]
        print(f"  {label} ({len(month_days)} missing)")
        for day in month_days:
            print(f"    {day.isoformat()}  {csv_name_for(day)}")


def main() -> None:
    client = get_box_client()
    years = list_year_folders(client)
    if not years:
        print(f"No year folders found under '{FILTERED_BASE}'.")
        return

    grand_total = 0
    for year in years:
        print(f"Checking {FILTERED_BASE}/{year} ...")
        missing = missing_dates_for_year(client, year)
        print_year_missing(year, missing)
        grand_total += len(missing)

    print(f"\n=== Total missing: {grand_total} days ===")


if __name__ == "__main__":
    main()
