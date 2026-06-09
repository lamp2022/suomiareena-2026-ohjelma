#!/usr/bin/env python3
"""
Parse SuomiAreena 2026 theme-day articles into per-slot programme events.

The official ohjelma lists some theme days only as a single umbrella placeholder
(e.g. "SuomiAreenan Turvallisuuspäivä") with no per-session breakdown or speakers.
These editorial articles publish the full hour-by-hour stage programme with
speakers. This script parses the 4 articles and writes theme_days.json, which
scrape.py merges into the build (enrich-if-empty / append-if-missing / drop the
superseded umbrella stub). Run when an article is published or updated:

  python3 parse_theme_days.py      # fetch + parse -> theme_days.json
"""

import json
import os
import re
import html as H
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "theme_days.json")
UA = "Mozilla/5.0 (compatible; SA-scraper/1.0; +theme-days)"

# article URL -> (date, stage). Verified against each article's intro line.
ARTICLES = {
    "https://www.suomiareena.fi/2026/06/suomiareenan-suuri-turvallisuuspaiva/": (
        "23.6.2026",
        "Raatihuoneenpuiston lava",
    ),
    "https://www.suomiareena.fi/2026/06/pohjoismaiden-paiva-miten-pohjola-menestyy-yhdessa/": (
        "24.6.2026",
        "Kaupungintalon pihan lava",
    ),
    "https://www.suomiareena.fi/2026/06/suomiareenan-koulupaiva-milla-osaamisella-suomi-parjaa-2030-luvulla/": (
        "25.6.2026",
        "Kaupungintalon pihan lava",
    ),
    "https://www.suomiareena.fi/2026/06/coolcationeista-guggenheimiin-tallainen-on-suomiareenan-matkailupaiva/": (
        "25.6.2026",
        "Paviljonki",
    ),
}

# Umbrella placeholder titles these articles supersede (dropped at build time).
SUPERSEDED = [
    "SuomiAreenan Turvallisuuspäivä",
    "SuomiAreenan Matkailupäivä",
    "SuomiAreenan Nordic Day",
]

SLOT = re.compile(r"^Klo\s*(\d{1,2})\.(\d{2})\s*[–-]\s*(\d{1,2})\.(\d{2})\s*(.*)$")
SPK_LABEL = re.compile(
    r"^(Keskustelijoina|Keskustelijat|Speakers|Puhujat|Mukana)\b\s*:?\s*(.*)$", re.I
)
STOP = re.compile(r"^(Ohjelmaan|Tutustu|Lue lisää|Katso|Seuraa|Huom|Ilmoittautu)", re.I)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def to_lines(htm):
    """Tags dropped WITHOUT inserting spaces so Office-paste span-split names
    (`<span>Johan</span><span>na </span>`) rejoin as `Johanna `."""
    m = re.search(r"entry-content[^>]*>(.*)", htm, re.S)
    body = m.group(1) if m else htm
    body = re.split(r'<(?:footer|div class="(?:sharedaddy|jp-relatedposts))', body)[0]
    body = re.sub(r"<(br|/p|/h\d|/li|/div|/tr)\b[^>]*>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = H.unescape(body)
    out = []
    for ln in body.split("\n"):
        ln = re.sub(r"[ \t ]+", " ", ln).strip()
        if ln:
            out.append(ln)
    return out


def split_speaker(raw):
    parts = [p.strip() for p in raw.strip(" ,").split(",")]
    parts = [p for p in parts if p]
    if not parts:
        return None
    name = parts[0]
    title = parts[1] if len(parts) > 1 else ""
    org = ", ".join(parts[2:]) if len(parts) > 2 else ""
    s = {"name": name}
    if title:
        s["title"] = title
    if org:
        s["organization"] = org
    return s


def parse(url):
    date, stage = ARTICLES[url]
    lines = to_lines(fetch(url))
    slots, cur, mode = [], None, None
    for ln in lines:
        m = SLOT.match(ln)
        if m:
            if cur:
                slots.append(cur)
            sh, sm, eh, em, rest = m.groups()
            cur = {
                "start": f"{int(sh)}.{sm}",
                "end": f"{int(eh)}.{em}",
                "title_parts": [rest.strip()] if rest.strip() else [],
                "spk_raw": [],
            }
            mode = "title"
            continue
        if cur is None:
            continue
        lab = SPK_LABEL.match(ln)
        if lab:
            mode = "spk"
            tail = lab.group(2).strip()
            if tail:
                cur["spk_raw"].append(tail)
            continue
        if STOP.match(ln):
            mode = None
            continue
        if mode == "title":
            cur["title_parts"].append(ln)
        elif mode == "spk":
            cur["spk_raw"].append(ln)
    if cur:
        slots.append(cur)

    events = []
    for s in slots:
        title = re.sub(r"\s+", " ", " ".join(s["title_parts"])).strip()
        speakers = [x for x in (split_speaker(r) for r in s["spk_raw"]) if x]
        events.append(
            {
                "post_id": None,
                "url": url,
                "title": title,
                "date": date,
                "time_range": f"{s['start']}–{s['end']}",
                "start_time": s["start"],
                "stage": stage,
                "organizer": "",
                "description": "",
                "speakers": speakers,
                "error": None,
                "source": "theme-day-article",
            }
        )
    return events


def main():
    all_events = []
    for url in ARTICLES:
        evs = parse(url)
        print(f"{url.split('/2026/06/')[1].rstrip('/')[:40]:42} {len(evs):2} slots")
        all_events.extend(evs)
    json.dump(
        {"superseded_titles": SUPERSEDED, "events": all_events},
        open(OUT, "w"),
        ensure_ascii=False,
        indent=1,
    )
    print(f"wrote {OUT} ({len(all_events)} slot events)")


if __name__ == "__main__":
    main()
