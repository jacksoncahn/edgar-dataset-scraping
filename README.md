# Overview

Accumulate DOJ and FTC transaction data by filtering EDGAR/SEC daily log datasets from **2003–2017**.

## Tech stack (brief)

- **Language:** Python 3
- **Scraping / HTTP:** `requests`, BeautifulSoup
- **Storage / cloud:** Box API via `boxsdk`
- **Data:** CSV (EDGAR log zips → CSV); filtering and validation in pure Python
- **Scraping progress management:** Periodically updating JSON files

## Python packages used

- `beautifulsoup4`
- `boxsdk>=3.9.2,<4`
- `python-dotenv`
- `requests`

# Process

### Scraping

Scraped from EDGAR’s daily transaction log datasets, per year:

https://www.sec.gov/data-research/sec-markets-data/edgar-log-file-data-sets

### Uploading

Uploaded to Box.com — both the **original** and **filtered** versions of each daily log.

### Filtering

Filtered rows by IP address against FTC and DOJ ranges. Because the last octet is obscured in EDGAR logs, some matches are labeled `doj_uncertain` (they *could* be DOJ).

### Testing

- **Unfiltered:** line-by-line row matching of Box files against freshly scraped EDGAR CSVs
- **Filtered:** membership and completeness checks against the unfiltered counterparts

## IP address ranges

```text
FTC:
  164.62.0.0 – 164.62.255.255

DOJ:
  149.101.0.0 – 149.101.255.255
  192.58.200.0 – 192.58.203.255
  63.145.235.0 – 63.145.235.255   (doj_uncertain)
```

# How to use this scraper

Coming soon.

# DIY

To reproduce identical results: scrape each daily CSV from the EDGAR year links (2003–2017), then filter by the FTC and DOJ IP ranges above.
