#!/usr/bin/env python3
"""Daily job: builds data/index.json and data/days/*.json for the brief.

Each source fails independently and soft. If a fetch fails, we fall back to
the last good cached value rather than overwriting it with an empty result.
"""
import html
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber

CENTRAL = ZoneInfo("America/Chicago")
WINDOW_BACK_DAYS = 7
WINDOW_FORWARD_DAYS = 90
USER_AGENT = "DailyOrthodoxBrief/1.0 (personal use; contact aimee)"
REQUEST_TIMEOUT = 15

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DAYS_DIR = DATA_DIR / "days"

ORTHOCAL_URL = "https://orthocal.info/api/greek/gregorian/{year}/{month}/{day}/"
PARISH_EVENTS_URL = "https://www.constantinehelen.com/wp-json/tribe/events/v1/events"
PARISH_MEDIA_URL = "https://www.constantinehelen.com/wp-json/wp/v2/media"
PARISH_HOME_URL = "https://www.constantinehelen.com/"

STATIC_LINKS = {
    "liturgy": "https://www.antiochian.org/liturgy",
    "vespers": "https://www.antiochian.org/vespers",
    "parish_calendar": "https://www.constantinehelen.com/calendar/",
}


def today_central() -> date:
    return datetime.now(CENTRAL).date()


def date_range(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return resp.read()


def http_get_json(url: str):
    return json.loads(http_get(url))


# ---------------------------------------------------------------------------
# Source: orthocal.info (liturgical data)
# ---------------------------------------------------------------------------

def fetch_liturgical_day(d: date) -> dict:
    url = ORTHOCAL_URL.format(year=d.year, month=d.month, day=d.day)
    raw = http_get_json(url)

    readings = []
    for r in raw.get("readings") or []:
        text = " ".join(
            (verse.get("content") or "").strip()
            for verse in (r.get("passage") or [])
        ).strip()
        readings.append({
            "source": r.get("source"),
            "display": r.get("display"),
            "short_display": r.get("short_display"),
            "text": text,
        })

    saints = []
    for name in raw.get("saints") or []:
        saints.append({
            "name": name,
            "link": "https://orthodoxwiki.org/Special:Search?search=" + urllib.parse.quote(name),
        })

    fast_level = raw.get("fast_level") or 0
    fasting = None
    if fast_level:
        fasting = {
            "level": fast_level,
            "description": raw.get("fast_level_desc"),
            "abstentions": raw.get("fast_abstentions") or [],
        }

    return {
        "status": "ok",
        "titles": raw.get("titles") or [],
        "summary_title": raw.get("summary_title"),
        "feasts": raw.get("feasts"),
        "saints": saints,
        "fasting": fasting,
        "readings": readings,
    }


# ---------------------------------------------------------------------------
# Source: constantinehelen.com parish events (The Events Calendar REST API)
# ---------------------------------------------------------------------------

def fetch_parish_events(start: date, end: date) -> dict:
    """Returns {date_str: [event, ...]} for every date in [start, end]."""
    events_by_date: dict[str, list] = {d.isoformat(): [] for d in date_range(start, end)}

    page = 1
    while True:
        url = (
            f"{PARISH_EVENTS_URL}?start_date={start.isoformat()}"
            f"&end_date={end.isoformat()}&per_page=100&page={page}"
        )
        raw = http_get_json(url)
        for ev in raw.get("events") or []:
            start_date = (ev.get("start_date") or "")[:10]
            if start_date not in events_by_date:
                continue
            events_by_date[start_date].append({
                "title": html.unescape(ev.get("title") or ""),
                "start": ev.get("start_date"),
                "end": ev.get("end_date"),
                "url": ev.get("url"),
            })
        total_pages = raw.get("total_pages") or 1
        if page >= total_pages:
            break
        page += 1

    return events_by_date


# ---------------------------------------------------------------------------
# Source: constantinehelen.com bulletin (WP media library)
# ---------------------------------------------------------------------------

def fetch_latest_bulletin() -> dict:
    url = f"{PARISH_MEDIA_URL}?search=bulletin&orderby=date&order=desc&per_page=1"
    raw = http_get_json(url)
    if not raw:
        raise ValueError("no bulletin found")
    item = raw[0]
    return {
        "url": item.get("source_url"),
        "posted": item.get("date"),
    }


# ---------------------------------------------------------------------------
# Source: prayer list (extracted from the bulletin PDF text)
# ---------------------------------------------------------------------------

PRAYER_LIST_PATTERN = re.compile(
    r"please\s+pray\s+for\s+the\s+health\s+of\s+body\s+and\s+soul\s+of:?\s*(?P<sick>.*?)\s*;\s*"
    r"for\s+those\s+serving\s+in[^:]*:\s*(?P<military>.*?)\s*;\s*"
    r"(?:and\s+)?for\s+our\s+catechumens:?\s*(?P<catechumens>.*?)\s*\.",
    re.IGNORECASE | re.DOTALL,
)


def split_names(blob: str) -> list[str]:
    names = [n.strip() for n in blob.replace("\n", " ").split(",")]
    names = [n for n in names if n]
    if names and names[-1].lower().startswith("and "):
        names[-1] = names[-1][4:].strip()
    return names


def find_prayer_box_text(pdf) -> str:
    """The prayer list sits in a boxed column next to unrelated text (e.g. the
    Lord's Prayer transliteration). Extracting a whole page's text interleaves
    the two side-by-side columns line by line, so instead we locate the
    anchor phrase by word position and crop just that box before extracting.
    """
    for page in pdf.pages:
        words = page.extract_words()
        for w0, w1 in zip(words, words[1:]):
            if w0["text"].lower() == "please" and w1["text"].lower() == "pray" and abs(w0["top"] - w1["top"]) < 3:
                px0, py0, px1, py1 = page.bbox
                crop_box = (
                    max(px0, w0["x0"] - 5),
                    max(py0, w0["top"] - 5),
                    px1,
                    min(py1, w0["top"] + 320),
                )
                return page.crop(crop_box).extract_text() or ""
    raise ValueError("prayer list box not found in bulletin")


def fetch_prayer_list(bulletin_url: str) -> dict:
    pdf_bytes = http_get(bulletin_url)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = find_prayer_box_text(pdf)

    match = PRAYER_LIST_PATTERN.search(text)
    if not match:
        raise ValueError("prayer list paragraph not found in bulletin text")

    return {
        "sick": split_names(match.group("sick")),
        "military": split_names(match.group("military")),
        "catechumens": split_names(match.group("catechumens")),
        "source_bulletin_url": bulletin_url,
    }


# ---------------------------------------------------------------------------
# Source: fasting calendar PDF link (scraped from parish homepage)
# ---------------------------------------------------------------------------

def fetch_fasting_calendar_link() -> str:
    body = http_get(PARISH_HOME_URL).decode("utf-8", errors="replace")
    match = re.search(r'href="([^"]*Fasting[^"]*Calendar[^"]*\.pdf)"', body, re.IGNORECASE)
    if not match:
        raise ValueError("fasting calendar link not found on parish homepage")
    return html.unescape(match.group(1))


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def day_file_path(d: date) -> Path:
    return DAYS_DIR / f"{d.isoformat()}.json"


def build_day_files(window_start: date, window_end: date) -> tuple[str, bool, bool]:
    """Returns (generated_at timestamp, any_liturgical_ok, any_events_ok)."""
    now_iso = datetime.now(CENTRAL).isoformat()

    events_by_date = None
    events_ok = False
    try:
        events_by_date = fetch_parish_events(window_start, window_end)
        events_ok = True
    except Exception as exc:
        print(f"[warn] parish events fetch failed: {exc}", file=sys.stderr)

    any_liturgical_ok = False
    for d in date_range(window_start, window_end):
        existing = load_json(day_file_path(d))
        liturgical = None

        if existing and existing.get("liturgical", {}).get("status") == "ok":
            liturgical = existing["liturgical"]
            any_liturgical_ok = True
        else:
            try:
                liturgical = fetch_liturgical_day(d)
                any_liturgical_ok = True
            except Exception as exc:
                print(f"[warn] liturgical fetch failed for {d}: {exc}", file=sys.stderr)
                liturgical = (existing or {}).get("liturgical") or {"status": "error"}

        if events_ok:
            events = events_by_date.get(d.isoformat(), [])
        else:
            events = (existing or {}).get("events") or []

        day_data = {
            "date": d.isoformat(),
            "liturgical": liturgical,
            "events": events,
        }
        day_file_path(d).write_text(json.dumps(day_data, indent=2))

    return now_iso, any_liturgical_ok, events_ok


def prune_old_day_files(window_start: date, window_end: date):
    for path in DAYS_DIR.glob("*.json"):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if d < window_start or d > window_end:
            path.unlink()


def main():
    DAYS_DIR.mkdir(parents=True, exist_ok=True)

    today = today_central()
    window_start = today - timedelta(days=WINDOW_BACK_DAYS)
    window_end = today + timedelta(days=WINDOW_FORWARD_DAYS)

    prior_index = load_json(DATA_DIR / "index.json") or {}
    prior_last_successful = prior_index.get("last_successful", {})
    prior_links = prior_index.get("links", {})

    now_iso, liturgical_ok, events_ok = build_day_files(window_start, window_end)
    prune_old_day_files(window_start, window_end)

    try:
        bulletin = fetch_latest_bulletin()
        bulletin_ok = True
    except Exception as exc:
        print(f"[warn] bulletin fetch failed: {exc}", file=sys.stderr)
        bulletin = None
        bulletin_ok = False

    try:
        fasting_calendar_link = fetch_fasting_calendar_link()
        fasting_calendar_ok = True
    except Exception as exc:
        print(f"[warn] fasting calendar link fetch failed: {exc}", file=sys.stderr)
        fasting_calendar_link = None
        fasting_calendar_ok = False

    links = dict(STATIC_LINKS)
    links["bulletin_pdf"] = (bulletin or {}).get("url") or prior_links.get("bulletin_pdf")
    links["bulletin_posted"] = (bulletin or {}).get("posted") or prior_links.get("bulletin_posted")
    links["fasting_calendar_pdf"] = fasting_calendar_link or prior_links.get("fasting_calendar_pdf")

    prayer_list = prior_index.get("prayer_list")
    prayer_list_ok = False
    if links["bulletin_pdf"]:
        try:
            prayer_list = fetch_prayer_list(links["bulletin_pdf"])
            prayer_list_ok = True
        except Exception as exc:
            print(f"[warn] prayer list extraction failed: {exc}", file=sys.stderr)

    last_successful = dict(prior_last_successful)
    if liturgical_ok:
        last_successful["liturgical"] = now_iso
    if events_ok:
        last_successful["parish_events"] = now_iso
    if bulletin_ok:
        last_successful["bulletin"] = now_iso
    if fasting_calendar_ok:
        last_successful["fasting_calendar"] = now_iso
    if prayer_list_ok:
        last_successful["prayer_list"] = now_iso

    available_dates = sorted(p.stem for p in DAYS_DIR.glob("*.json"))

    index = {
        "generated_at": now_iso,
        "today": today.isoformat(),
        "available_dates": available_dates,
        "last_successful": last_successful,
        "links": links,
        "prayer_list": prayer_list,
        "status": {
            "liturgical_ok": liturgical_ok,
            "parish_events_ok": events_ok,
            "bulletin_ok": bulletin_ok,
            "fasting_calendar_ok": fasting_calendar_ok,
            "prayer_list_ok": prayer_list_ok,
        },
    }
    (DATA_DIR / "index.json").write_text(json.dumps(index, indent=2))

    print(f"Wrote {len(available_dates)} day files. Today: {today.isoformat()}")
    print(f"liturgical_ok={liturgical_ok} parish_events_ok={events_ok} "
          f"bulletin_ok={bulletin_ok} fasting_calendar_ok={fasting_calendar_ok} "
          f"prayer_list_ok={prayer_list_ok}")


if __name__ == "__main__":
    main()
