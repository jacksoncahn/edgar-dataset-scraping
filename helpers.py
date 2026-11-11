import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
MANAGEMENT_FILE = BASE_DIR / "async_management.json"


def default_management() -> dict:
    return {
        "baseline_links": {
            "2003": "https://www.sec.gov/files/edgar2003.html",
            "2004": "https://www.sec.gov/files/edgar2004.html",
            "2005": "https://www.sec.gov/files/edgar2005.html",
            "2006": "https://www.sec.gov/files/edgar2006.html",
            "2007": "https://www.sec.gov/files/edgar2007.html",
            "2008": "https://www.sec.gov/files/edgar2008.html",
            "2009": "https://www.sec.gov/files/edgar2009.html",
            "2010": "https://www.sec.gov/files/edgar2010.html",
            "2011": "https://www.sec.gov/files/edgar2011.html",
            "2012": "https://www.sec.gov/files/edgar2012.html",
            "2013": "https://www.sec.gov/files/edgar2013.html",
            "2014": "https://www.sec.gov/files/edgar2014.html",
            "2015": "https://www.sec.gov/files/edgar2015.html",
            "2016": "https://www.sec.gov/files/edgar2016.html",
            "2017": "https://www.sec.gov/files/edgar2017.html",
        },
        "current": {
            "current_index": 0,
            "year": "2016",
            "links": [],
        },
        "test_box": True,
        "failed_files": [],
    }


def load_management(path=MANAGEMENT_FILE):
    if not Path(path).exists():
        data = default_management()
        save_management(data, path=path)
        return data

    raw = Path(path).read_text(encoding="utf-8").strip()
    if not raw:
        data = default_management()
        save_management(data, path=path)
        return data

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        data = default_management()
        save_management(data, path=path)
        return data


def save_management(data, path=MANAGEMENT_FILE):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def build_links(links_list):

    """
    takes in a list of links and stores it as the link list for the current
    year (current.links).
    """

    async_management = load_management()

    async_management["current"]["links"] = links_list

    save_management(async_management)

    return


def increment(path=MANAGEMENT_FILE):
    """
    advances current_index within current.links. Returns "complete" when the
    last link of the current year has been reached, otherwise returns the new
    index/link.
    """
    data = load_management(path=path)
    links = data["current"].get("links", [])

    current_index = int(data["current"].get("current_index", 0))

    if current_index >= len(links) - 1:
        return "complete"

    current_index += 1
    data["current"]["current_index"] = current_index
    save_management(data, path=path)

    return {
        "status": "incomplete",
        "current_index": current_index,
        "link": links[current_index],
    }


def create_link(index=None, path=MANAGEMENT_FILE):
    """
    gets from the stored list of links in current.links and indexes it based on
    current_index to get the link to download the file from.
    """
    data = load_management(path=path)
    links = data["current"].get("links", [])

    if index is None:
        index = int(data["current"].get("current_index", 0))

    if index < 0 or index >= len(links):
        raise IndexError("Link index is out of range")

    return links[index]


def log_failed_file(link, reason, path=MANAGEMENT_FILE):
    """Append a failed link + reason to failed_files in async_management.json."""
    data = load_management(path=path)
    data.setdefault("failed_files", []).append({"link": link, "reason": reason})
    save_management(data, path=path)



