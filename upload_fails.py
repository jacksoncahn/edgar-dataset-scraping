"""Retry failed EDGAR zip downloads via process_and_upload."""

from box.box_upload import get_box_client
from webscraper_unfiltered import HEADERS, process_and_upload

# Corrected-quarter retries for the previous scrape 403s.
LINKS = [
    # 2016-08-20 -> Qtr3
    "http://www.sec.gov/dera/data/Public-EDGAR-log-file-data/2016/Qtr3/log20160820.zip",
    # 2008-08-10 -> Qtr3
    "http://www.sec.gov/dera/data/Public-EDGAR-log-file-data/2008/Qtr3/log20080810.zip",
]

if __name__ == "__main__":
    client = get_box_client()

    for link in LINKS:
        file_name = link.rsplit("/", 1)[-1]
        print(f"Uploading {file_name}")
        ok, stage, reason, client = process_and_upload(link, client, headers=HEADERS)
        if ok:
            print("    success")
        else:
            print(f"    failed ({stage}): {reason}")
