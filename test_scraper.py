"""Smoke-test the full scrape -> Box upload pipeline on one EDGAR zip."""

from box_upload import get_box_client
from webscraper import HEADERS, process_and_upload

TEST_ZIP_LINK = (
    "https://www.sec.gov/dera/data/Public-EDGAR-log-file-data/2003/Qtr4/log20031231.zip"
)

if __name__ == "__main__":
    print(f"Testing pipeline on: {TEST_ZIP_LINK}")
    client = get_box_client()
    ok, stage, reason = process_and_upload(TEST_ZIP_LINK, client, headers=HEADERS)

    if ok:
        print("SUCCESS: scrape + Box upload completed")
    else:
        print(f"FAILED at {stage}: {reason}")
        raise SystemExit(1)
