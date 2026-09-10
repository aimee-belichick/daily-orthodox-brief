#!/usr/bin/env python3
"""Daily job: builds data/index.json and data/days/*.json for the brief.

Each source fails independently and soft. If a fetch fails, we fall back to
the last good cached value rather than overwriting it with an empty result.
"""
from __future__ import annotations

import html
import io
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber
from striprtf.striprtf import rtf_to_text
from playwright.sync_api import sync_playwright

CENTRAL = ZoneInfo("America/Chicago")
WINDOW_BACK_DAYS = 7
WINDOW_FORWARD_DAYS = 90
USER_AGENT = "DailyOrthodoxBrief/1.0 (personal use; contact aimee)"
REQUEST_TIMEOUT = 15

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DAYS_DIR = DATA_DIR / "days"

ORTHOCAL_URL = "https://orthocal.info/api/greek/gregorian/{year}/{month}/{day}/"
ANTIOCHIAN_BASE = "https://www.antiochian.org"
PARISH_EVENTS_URL = "https://www.constantinehelen.com/wp-json/tribe/events/v1/events"
PARISH_MEDIA_URL = "https://www.constantinehelen.com/wp-json/wp/v2/media"
PARISH_HOME_URL = "https://www.constantinehelen.com/"

STATIC_LINKS = {
    "parish_calendar": "https://www.constantinehelen.com/calendar/",
}


def today_central() -> date:
    return datetime.now(CENTRAL).date()


# ---------------------------------------------------------------------------
# The Twelve Great Feasts + Pascha
#
# antiochian.org's page doesn't flag which commemorations are Great Feasts,
# so we identify them by matching known feast names against the actual
# scraped title/commemoration text for that day (whichever source produced
# it), rather than computing dates independently.
# ---------------------------------------------------------------------------

# Each entry: (canonical name, is_pascha, [word-groups; any group matching is a hit]).
# Word groups include known naming variants across Orthodox jurisdictions/sources
# (e.g. antiochian.org says "Elevation" where others say "Exaltation").
GREAT_FEAST_PATTERNS = [
    ("Pascha", True, [["pascha"], ["resurrection", "christ"]]),
    ("Entry of the Lord into Jerusalem (Palm Sunday)", False, [["palm", "sunday"], ["entry", "jerusalem"]]),
    ("Ascension", False, [["ascension"]]),
    ("Pentecost", False, [["pentecost"]]),
    ("Theophany", False, [["theophany"], ["epiphany"], ["baptism", "christ"]]),
    ("Nativity of the Theotokos", False, [["nativity", "theotokos"], ["birth", "theotokos"]]),
    ("Exaltation of the Holy Cross", False, [["exaltation", "cross"], ["elevation", "cross"]]),
    ("Presentation of the Theotokos in the Temple", False, [
        ["entrance", "theotokos", "temple"],
        ["presentation", "theotokos", "temple"],
    ]),
    ("Nativity of Christ", False, [["nativity", "christ"], ["birth", "christ"], ["christmas"]]),
    ("Presentation of Christ in the Temple", False, [["presentation", "temple"], ["meeting", "lord"]]),
    ("Annunciation", False, [["annunciation"]]),
    ("Transfiguration", False, [["transfiguration"]]),
    ("Dormition of the Theotokos", False, [["dormition"], ["falling", "asleep", "theotokos"]]),
]


FEAST_PERIOD_REFERENCE_PATTERN = re.compile(
    r"(fore|after)feast of [^,;]*|leave-?taking of [^,;]*|apodosis of [^,;]*",
    re.IGNORECASE,
)
# "Nth Sunday/week/etc. after Pentecost" is the routine way antiochian.org labels
# ordinary weeks, not a reference to the Feast of Pentecost itself.
ORDINARY_TIME_PATTERN = re.compile(r"after pentecost", re.IGNORECASE)


def identify_great_feast(text_blob: str) -> dict | None:
    # Strip "afterfeast of X" / "forefeast of X" / "leave-taking of X" / "apodosis of X"
    # clauses first, so a day within a feast's surrounding period isn't mistaken for
    # the feast itself, and strip routine "N weeks after Pentecost" week-numbering.
    text = FEAST_PERIOD_REFERENCE_PATTERN.sub("", text_blob or "")
    text = ORDINARY_TIME_PATTERN.sub("", text).lower()
    for name, is_pascha, groups in GREAT_FEAST_PATTERNS:
        if name == "Presentation of Christ in the Temple" and "theotokos" in text:
            continue  # avoid clashing with the Theotokos presentation/entrance feast
        if any(all(word in text for word in group) for group in groups):
            return {"name": name, "is_pascha": is_pascha}
    return None


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


def fetch_rtf_text(url: str) -> str:
    raw = http_get(url).decode("cp1252", errors="replace")
    text = rtf_to_text(raw).strip()
    if not text:
        raise ValueError("RTF converted to empty text")
    return text


# ---------------------------------------------------------------------------
# Source: antiochian.org (primary liturgical data, via headless browser)
#
# The site is a client-rendered Angular app, so plain HTTP fetching returns an
# empty shell. Each day's page is addressable by a sequential integer ID
# (confirmed: id N = id of today +/- N days), which lets us navigate directly
# instead of driving the on-page date picker.
# ---------------------------------------------------------------------------

DATE_HEADING_PATTERN = re.compile(r"([A-Z]+),\s*([A-Z]+)\s+(\d+),\s*(\d{4})")
MONTH_NAMES = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
               "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]

TITLECASE_LOWERCASE_WORDS = {"of", "and", "the", "in", "on", "for", "to", "a", "an", "at", "by", "with"}


def titlecase(text: str) -> str:
    """antiochian.org stores these strings in ALL CAPS; render them more readably."""
    words = text.title().split(" ")
    fixed = []
    for i, w in enumerate(words):
        w = re.sub(r"'([A-Z])", lambda m: "'" + m.group(1).lower(), w)
        lw = w.lower()
        fixed.append(lw if i != 0 and lw in TITLECASE_LOWERCASE_WORDS else w)
    return " ".join(fixed)


def parse_date_heading(body_text: str) -> date:
    m = DATE_HEADING_PATTERN.search(body_text)
    if not m:
        raise ValueError("date heading not found on antiochian.org page")
    month = MONTH_NAMES.index(m.group(2)) + 1
    return date(int(m.group(4)), month, int(m.group(3)))


def antiochian_find_today_id(page) -> int:
    page.goto(f"{ANTIOCHIAN_BASE}/liturgicday", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1200)
    href = page.eval_on_selector("a[href*='/epistleliturgicday/']", "el => el.getAttribute('href')")
    m = re.search(r"/epistleliturgicday/(\d+)", href or "")
    if not m:
        raise ValueError("could not find today's antiochian.org id")
    found_date = parse_date_heading(page.inner_text("body"))
    if found_date != today_central():
        raise ValueError(f"antiochian.org today mismatch: page says {found_date}")
    return int(m.group(1))


def antiochian_parse_liturgicday(page, id_: int, expected_date: date) -> dict:
    page.goto(f"{ANTIOCHIAN_BASE}/liturgicday/{id_}", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1200)

    body_text = page.inner_text("body")
    found_date = parse_date_heading(body_text)
    if found_date != expected_date:
        raise ValueError(f"antiochian.org id {id_} resolved to {found_date}, expected {expected_date}")

    items = page.eval_on_selector_all(".liturgicalDayListItem", """els => els.map(el => ({
        hasLink: !!el.querySelector('a'),
        title: (el.querySelector('.liturgicalSubItemTitle') || {}).innerText || null,
        desc: (el.querySelector('.liturgicalSubItemDesc') || {}).innerText || null,
        iconSrc: (el.querySelector('img') || {}).src || ""
    }))""")

    feast_item = next((it for it in items if not it["hasLink"] and "feast" in it["iconSrc"].lower()), None)
    fasting_item = next((it for it in items if not it["hasLink"] and "fasting" in it["iconSrc"].lower()), None)

    if not feast_item or not feast_item["title"]:
        raise ValueError(f"antiochian.org id {id_}: feast title not found")

    fasting = None
    if fasting_item and fasting_item["title"]:
        text = fasting_item["title"].strip()
        if text.upper() != "NO FAST":
            abstentions = [a.strip().lower() for a in re.sub(r"(?i)^abstain from\s*", "", text).split(",") if a.strip()]
            fasting = {"description": titlecase(text), "abstentions": abstentions}

    service_links_raw = page.eval_on_selector_all(
        "a.dailyLiturgicalTextUrl",
        "els => els.map(e => ({text: e.innerText.trim(), href: e.href}))",
    )
    by_label = {}
    for link in service_links_raw:
        if link["text"].endswith("(PDF)"):
            by_label.setdefault(link["text"][:-len("(PDF)")].strip(), {})["pdf_url"] = link["href"]
        elif link["text"].endswith("(RTF)"):
            by_label.setdefault(link["text"][:-len("(RTF)")].strip(), {})["rtf_url"] = link["href"]

    service_texts = []
    for label, urls in by_label.items():
        entry = {"label": label, "pdf_url": urls.get("pdf_url")}
        if urls.get("rtf_url"):
            try:
                entry["text"] = fetch_rtf_text(urls["rtf_url"])
            except Exception as exc:
                print(f"[warn] RTF fetch failed for {label!r} ({urls['rtf_url']}): {exc}", file=sys.stderr)
        service_texts.append(entry)

    commemorations = [titlecase(c.strip()) for c in (feast_item["desc"] or "").split(",") if c.strip()]

    return {
        "title": titlecase(feast_item["title"].strip()),
        "commemorations": commemorations,
        "fasting": fasting,
        "service_texts": service_texts,
    }


def antiochian_parse_readings(page, id_: int) -> list:
    page.goto(f"{ANTIOCHIAN_BASE}/epistleliturgicday/{id_}", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(1200)

    pairs = page.eval_on_selector_all(".dailyReadingSubTitle", "els => els.map(e => e.innerText.trim())")
    texts = page.eval_on_selector_all(".dailyReadingSubDesc", "els => els.map(e => e.innerText.trim())")
    return [
        {"display": titlecase(title), "text": text}
        for title, text in zip(pairs, texts)
        if title and text
    ]


def fetch_antiochian_day(page, id_: int, expected_date: date) -> dict:
    day_info = antiochian_parse_liturgicday(page, id_, expected_date)
    readings = antiochian_parse_readings(page, id_)
    if not readings:
        raise ValueError(f"antiochian.org id {id_}: no readings found")

    return {
        "status": "ok",
        "source": "antiochian",
        "feasts": [day_info["title"]],
        "saints": day_info["commemorations"],
        "summary_title": ", ".join(day_info["commemorations"]) or day_info["title"],
        "stories": [],
        "fasting": day_info["fasting"],
        "readings": readings,
        "service_texts": day_info["service_texts"],
    }


# ---------------------------------------------------------------------------
# Source: orthocal.info (fallback liturgical data)
# ---------------------------------------------------------------------------

def fetch_orthocal_day(d: date) -> dict:
    url = ORTHOCAL_URL.format(year=d.year, month=d.month, day=d.day)
    raw = http_get_json(url)

    readings = []
    for r in raw.get("readings") or []:
        text = " ".join(
            (verse.get("content") or "").strip()
            for verse in (r.get("passage") or [])
        ).strip()
        readings.append({
            "display": r.get("display"),
            "text": text,
        })

    stories = [
        {"title": s.get("title"), "html": s.get("story")}
        for s in (raw.get("stories") or [])
        if s.get("story")
    ]

    fast_level = raw.get("fast_level") or 0
    fasting = None
    if fast_level:
        fasting = {
            "description": raw.get("fast_level_desc"),
            "abstentions": raw.get("fast_abstentions") or [],
        }

    return {
        "status": "ok",
        "source": "orthocal",
        "feasts": raw.get("feasts"),
        "saints": raw.get("saints") or [],
        "summary_title": raw.get("summary_title"),
        "stories": stories,
        "fasting": fasting,
        "readings": readings,
        "service_texts": [],
    }


def fetch_liturgical_day(page, id_map: dict, d: date) -> dict:
    """Try antiochian.org first (via headless browser); fall back to orthocal.info."""
    try:
        return fetch_antiochian_day(page, id_map[d], d)
    except Exception as exc:
        print(f"[warn] antiochian.org fetch failed for {d}: {exc}", file=sys.stderr)
    return fetch_orthocal_day(d)


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

    dates_needing_liturgical = []
    for d in date_range(window_start, window_end):
        existing = load_json(day_file_path(d))
        if not (existing and existing.get("liturgical", {}).get("status") == "ok"):
            dates_needing_liturgical.append(d)

    liturgical_by_date = {}
    any_liturgical_ok = False
    if dates_needing_liturgical:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(user_agent=USER_AGENT)
            try:
                today_id = antiochian_find_today_id(page)
                today = today_central()
                id_map = {d: today_id + (d - today).days for d in dates_needing_liturgical}
            except Exception as exc:
                print(f"[warn] antiochian.org unavailable this run: {exc}", file=sys.stderr)
                id_map = {}

            for d in dates_needing_liturgical:
                try:
                    liturgical_by_date[d] = fetch_liturgical_day(page, id_map, d) if d in id_map else fetch_orthocal_day(d)
                    any_liturgical_ok = True
                except Exception as exc:
                    print(f"[warn] liturgical fetch failed for {d}: {exc}", file=sys.stderr)
            browser.close()

    for d in date_range(window_start, window_end):
        existing = load_json(day_file_path(d))

        if d in liturgical_by_date:
            liturgical = liturgical_by_date[d]
        elif existing and existing.get("liturgical", {}).get("status") == "ok":
            liturgical = existing["liturgical"]
            any_liturgical_ok = True
        else:
            liturgical = (existing or {}).get("liturgical") or {"status": "error"}

        if liturgical.get("status") == "ok":
            text_blob = " ".join(filter(None, [liturgical.get("summary_title")] + (liturgical.get("feasts") or [])))
            liturgical["great_feast"] = identify_great_feast(text_blob)

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
