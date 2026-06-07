#!/usr/bin/env python3
"""
SuomiAreena 2026 ohjelma scraper.

Principles mirrored from Kohdescraper (private repo SPEC/AGENTS):
  - HTTP fetch + HTML parse (no headless browser; page is server-rendered WP).
  - Checkpointed, resumable batches: detail pages append to events.jsonl,
    already-fetched post-ids are skipped on restart.
  - Polite jittered crawl between detail fetches.
  - Normalizer maps raw markup into one canonical event shape.
  - No silent errors: a failed detail fetch is recorded with its reason, not swallowed.
  - Conditional-required fields: a field absent in the source is empty, not a failure.

Usage:
  python3 scrape.py            # parse list + fetch missing detail pages
  python3 scrape.py --build    # (re)assemble events.json + ohjelma_table.html from checkpoint
"""

import json
import os
import re
import sys
import time
import html
import random
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
LIST_HTML = os.path.join(HERE, "ohjelma_raw.html")
CHECKPOINT = os.path.join(HERE, "events.jsonl")
FINAL_JSON = os.path.join(HERE, "events.json")
TABLE_HTML = os.path.join(HERE, "ohjelma_table.html")  # legacy combined page
INDEX_HTML = os.path.join(HERE, "index.html")  # single page, Ohjelma/Puhujat toggle
OHJELMA_HTML = os.path.join(HERE, "ohjelma.html")  # redirect -> index.html#ohjelma
PUHUJAT_HTML = os.path.join(HERE, "puhujat.html")  # redirect -> index.html#puhujat

LIST_URL = "https://www.suomiareena.fi/suomiareena-2026/ohjelma/"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 scrape-suomiareena/1.0"
PERSON_PLACEHOLDER = "person.svg"

# --- tiny helpers -----------------------------------------------------------


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    """Strip tags, unescape entities, collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# --- list page → event stubs ------------------------------------------------

ARTICLE_RE = re.compile(r'<article id="post-(\d+)"[^>]*>(.*?)</article>', re.S)


def parse_list(html_text):
    """Return list of stub dicts from the ohjelma list page."""
    events = []
    for m in ARTICLE_RE.finditer(html_text):
        post_id, body = m.group(1), m.group(2)

        title_m = re.search(
            r'class="entry-title event-title"><a href="([^"]+)"[^>]*>(.*?)</a>',
            body,
            re.S,
        )
        url = title_m.group(1) if title_m else ""
        title = clean(title_m.group(2)) if title_m else ""

        date_m = re.search(r'<time class="event-date">(.*?)</time>', body, re.S)
        time_m = re.search(r'<time class="event-time">(.*?)</time>', body, re.S)
        loc_m = re.search(r'<div class="location">(.*?)</div>', body, re.S)
        org_m = re.search(r'<div class="organizer">(.*?)</div>', body, re.S)

        date = clean(date_m.group(1)) if date_m else ""
        time_range = clean(time_m.group(1)) if time_m else ""
        # normalize em/en dashes to a plain en-dash for the range
        time_range = time_range.replace("—", "–").replace("--", "–")
        start_time = time_range.split("–")[0].strip() if time_range else ""

        events.append(
            {
                "post_id": post_id,
                "url": url,
                "title": title,
                "date": date,
                "time_range": time_range,
                "start_time": start_time,
                "stage": clean(loc_m.group(1)) if loc_m else "",
                "organizer": clean(org_m.group(1)) if org_m else "",
            }
        )
    return events


# --- detail page → description + speakers ------------------------------------

SPEAKER_LI_RE = re.compile(r'<li class="people speaker">(.*?)</li>', re.S)


def parse_detail(html_text, title):
    """Return (description, speakers list). Conditional-required fields."""
    content_m = re.search(
        r'<div class="entry-content">(.*?)</div><!-- \.entry-content -->',
        html_text,
        re.S,
    )
    block = content_m.group(1) if content_m else ""

    # description = everything before the "Puhujat" heading
    desc_html = re.split(r"<h2>\s*Puhujat\s*</h2>", block)[0]
    # drop a trailing <hr> separator
    desc_html = re.sub(r"<hr\s*/?>\s*$", "", desc_html.strip())
    paras = re.findall(r"<p>(.*?)</p>", desc_html, re.S)
    description = "\n\n".join(clean(p) for p in paras if clean(p))
    if not description:
        description = clean(desc_html)

    speakers = []
    for li in SPEAKER_LI_RE.finditer(html_text):
        seg = li.group(1)
        name = (
            clean((re.search(r"<h3>(.*?)</h3>", seg, re.S) or [None, ""])[1])
            if re.search(r"<h3>(.*?)</h3>", seg, re.S)
            else ""
        )
        role = (
            clean(
                (
                    re.search(r'class="person-role">(.*?)</div>', seg, re.S)
                    or [None, ""]
                )[1]
            )
            if re.search(r'class="person-role">(.*?)</div>', seg, re.S)
            else ""
        )
        ptitle = (
            clean(
                (
                    re.search(r'class="person-title">(.*?)</div>', seg, re.S)
                    or [None, ""]
                )[1]
            )
            if re.search(r'class="person-title">(.*?)</div>', seg, re.S)
            else ""
        )
        org = (
            clean(
                (
                    re.search(r'class="person-organization">(.*?)</div>', seg, re.S)
                    or [None, ""]
                )[1]
            )
            if re.search(r'class="person-organization">(.*?)</div>', seg, re.S)
            else ""
        )
        photo = (
            re.search(r'class="person-photo"><img[^>]*src="([^"]+)"', seg) or [None, ""]
        )[1]

        # placeholder detection: name echoes event title, no real metadata, stock photo
        is_placeholder = (
            (not role and not ptitle and not org)
            and (PERSON_PLACEHOLDER in (photo or ""))
            and (name == title or not name)
        )
        if is_placeholder:
            continue
        if not name:
            continue
        speakers.append(
            {"name": name, "role": role, "title": ptitle, "organization": org}
        )

    return description, speakers


# --- crawl (checkpointed) ----------------------------------------------------


def load_done():
    done = {}
    if os.path.exists(CHECKPOINT):
        with open(CHECKPOINT) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    done[e["post_id"]] = e
                except json.JSONDecodeError:
                    continue
    return done


def refetch_list():
    """Re-download the source list page so new/changed events are picked up."""
    print(f"downloading {LIST_URL}")
    open(LIST_HTML, "w").write(fetch(LIST_URL))


def crawl(refresh=False):
    if refresh:
        refetch_list()
    if not os.path.exists(LIST_HTML):
        print(f"missing {LIST_HTML}; download list page first", file=sys.stderr)
        sys.exit(1)
    stubs = parse_list(open(LIST_HTML).read())
    total = len(stubs)
    print(f"parsed {total} events from list page")

    done = load_done()
    print(f"checkpoint has {len(done)} done")

    # in refresh mode, force-refetch events still missing speakers (and any that errored)
    force = set()
    if refresh:
        force = {
            pid for pid, e in done.items() if not e.get("speakers") or e.get("error")
        }
        print(
            f"refresh: {force and len(force) or 0} events without speakers to refetch"
        )

    with open(CHECKPOINT, "a") as ckpt:
        for i, stub in enumerate(stubs, 1):
            pid = stub["post_id"]
            if pid in done and pid not in force:
                continue
            rec = dict(stub)
            try:
                page = fetch(stub["url"])
                desc, speakers = parse_detail(page, stub["title"])
                rec["description"] = desc
                rec["speakers"] = speakers
                rec["error"] = None
            except Exception as e:  # no silent errors: record reason
                rec["description"] = ""
                rec["speakers"] = []
                rec["error"] = f"{type(e).__name__}: {e}"
                print(
                    f"  ! {pid} {stub['title'][:40]} -> {rec['error']}", file=sys.stderr
                )
            ckpt.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ckpt.flush()
            done[pid] = rec  # latest record wins on next load_done()
            if i % 25 == 0 or i == total:
                print(f"{i}/{total} scanned")
            time.sleep(0.4 + random.random() * 0.6)  # polite jitter

    print("crawl complete")
    build()


# --- assemble final json + html table ---------------------------------------

DAY_NAMES = {
    "23.6.2026": "Ti 23.6.",
    "24.6.2026": "Ke 24.6.",
    "25.6.2026": "To 25.6.",
    "26.6.2026": "Pe 26.6.",
}

# 10-colour palette; cycles per distinct start-time within a day
PALETTE = [
    "#e8f1fb",
    "#fdefe3",
    "#e9f6ec",
    "#f6e9f3",
    "#fbf4e0",
    "#e5f4f6",
    "#f0eafb",
    "#f7e9ea",
    "#eef3e0",
    "#e8ecf7",
]


def _time_key(t):
    m = re.match(r"(\d{1,2})\.(\d{2})", t or "")
    return (int(m.group(1)), int(m.group(2))) if m else (99, 99)


def assemble():
    done = load_done()
    events = sorted(
        done.values(),
        key=lambda e: (
            e.get("date", ""),
            _time_key(e.get("start_time", "")),
            e.get("title", ""),
        ),
    )
    # stable day order
    order = {d: i for i, d in enumerate(DAY_NAMES)}
    events.sort(
        key=lambda e: (
            order.get(e.get("date", ""), 99),
            _time_key(e.get("start_time", "")),
            e.get("title", ""),
        )
    )
    return events


def build():
    events = assemble()
    with open(FINAL_JSON, "w") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    print(f"wrote {FINAL_JSON} ({len(events)} events)")
    write_pages(events)
    print(f"wrote {INDEX_HTML} (+ ohjelma.html/puhujat.html redirects)")


def esc(s):
    return html.escape(s or "")


def speakers_text(speakers):
    if not speakers:
        return '<span class="tbd">ei vielä tiedossa</span>'
    parts = []
    for s in speakers:
        meta = ", ".join(
            x for x in (s.get("role"), s.get("title"), s.get("organization")) if x
        )
        if meta:
            parts.append(f'{esc(s["name"])} <span class="meta">({esc(meta)})</span>')
        else:
            parts.append(esc(s["name"]))
    return "<br>".join(parts)


def _color_map(evs):
    """One colour per distinct start_time, in order of appearance."""
    color_of, ci = {}, 0
    for e in evs:
        st = e.get("start_time", "")
        if st not in color_of:
            color_of[st] = PALETTE[ci % len(PALETTE)]
            ci += 1
    return color_of


# shared stylesheet for both pages (mobile-first, responsive)
PAGE_CSS = """
  :root { --ink:#1a1a1a; --line:#e2e2e2; --muted:#666; --banner:#1a1a1a; --bannerH:40px; }
  * { box-sizing:border-box; }
  html { -webkit-text-size-adjust:100%; }
  body { font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
         color:var(--ink); margin:0; padding:0 16px 48px; background:#fafafa; }
  header.top { padding:18px 0 12px; border-bottom:1px solid var(--line); }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0; font-size:12.5px; }
  nav.tabs { margin:12px 0 0; display:flex; gap:8px; }
  nav.tabs a { font-size:14px; font-weight:600; text-decoration:none; color:var(--ink);
               padding:8px 18px; border:1px solid var(--line); border-radius:999px; background:#fff; }
  nav.tabs a.active { background:var(--ink); color:#fff; border-color:var(--ink); }
  section { margin:0 0 8px; }
  /* sticky day banner — keeps the date visible while scrolling a long day */
  .day { position:sticky; top:0; z-index:5; height:var(--bannerH); display:flex; align-items:center;
         gap:10px; background:var(--banner); color:#fff; padding:0 12px; font-size:16px; font-weight:700;
         letter-spacing:.02em; box-shadow:0 1px 0 rgba(0,0,0,.15); }
  .day .cnt { font-size:12px; font-weight:400; opacity:.75; }
  table { border-collapse:collapse; width:100%; background:#fff; box-shadow:0 1px 2px rgba(0,0,0,.06);
          margin:0 0 24px; }
  th, td { text-align:left; padding:9px 10px; border-bottom:1px solid var(--line); vertical-align:top; }
  th { background:#3a3a3a; color:#fff; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
       position:sticky; top:var(--bannerH); z-index:4; }
  td.t { white-space:nowrap; font-variant-numeric:tabular-nums; }
  td a { color:#0a52b5; text-decoration:none; }
  td a:hover { text-decoration:underline; }
  .meta { color:var(--muted); font-size:13px; }
  .tbd { color:#b00; font-style:italic; }
  .prog td.title, .spk td.title { font-weight:600; }
  .view[hidden] { display:none; }
  /* phones: tighter, smaller type, drop the lower-value "Aika-väli" column */
  @media (max-width:640px) {
    body { padding:0 10px 40px; font-size:14px; }
    h1 { font-size:19px; }
    th, td { padding:7px 7px; }
    th { font-size:10px; }
    .meta { font-size:12px; }
    .prog th:nth-child(2), .prog td:nth-child(2) { display:none; }  /* hide Aika-väli; full range stays in JSON + detail link */
  }
  @media print {
    .day { position:static; } th { position:static; } nav.tabs { display:none; }
    body { background:#fff; }
  }
"""


TOGGLE_JS = """
  const views = { ohjelma: document.getElementById('view-ohjelma'),
                  puhujat: document.getElementById('view-puhujat') };
  const tabs = document.querySelectorAll('nav.tabs a');
  function show(view) {
    if (!views[view]) view = 'ohjelma';
    for (const k in views) views[k].hidden = (k !== view);
    tabs.forEach(t => t.classList.toggle('active', t.dataset.view === view));
    document.title = (view === 'puhujat' ? 'SuomiAreena 2026 – Puhujat'
                                         : 'SuomiAreena 2026 – Ohjelma');
  }
  function fromHash() { show((location.hash || '#ohjelma').slice(1)); }
  window.addEventListener('hashchange', fromHash);
  fromHash();
"""


def _page(subtitle, ohjelma_sections, puhujat_sections):
    return f"""<!DOCTYPE html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SuomiAreena 2026 – Ohjelma</title>
<style>{PAGE_CSS}</style>
</head>
<body>
  <header class="top">
    <h1>SuomiAreena 2026</h1>
    <p class="sub">{esc(subtitle)}</p>
    <nav class="tabs">
      <a href="#ohjelma" data-view="ohjelma" class="active">Ohjelma</a>
      <a href="#puhujat" data-view="puhujat">Puhujat</a>
    </nav>
  </header>
  <div id="view-ohjelma" class="view">{"".join(ohjelma_sections)}</div>
  <div id="view-puhujat" class="view" hidden>{"".join(puhujat_sections)}</div>
  <script>{TOGGLE_JS}</script>
</body>
</html>"""


def write_pages(events):
    by_day = {}
    for e in events:
        by_day.setdefault(e.get("date", ""), []).append(e)

    ohjelma_sections, puhujat_sections = [], []
    for date in DAY_NAMES:
        evs = by_day.get(date, [])
        if not evs:
            continue
        color_of = _color_map(evs)
        banner = (
            f'<div class="day">{esc(DAY_NAMES[date])} '
            f'<span class="cnt">{esc(date)} · {len(evs)} tapahtumaa</span></div>'
        )

        # --- Ohjelma (programme, no speakers) ---
        prog_rows = []
        for e in evs:
            bg = color_of.get(e.get("start_time", ""), "#fff")
            link = (
                f'<a href="{esc(e.get("url"))}" target="_blank" rel="noopener">'
                f"{esc(e.get('title'))}</a>"
            )
            prog_rows.append(
                f'<tr style="background:{bg}">'
                f'<td class="t">{esc(e.get("start_time"))}</td>'
                f'<td class="t">{esc(e.get("time_range"))}</td>'
                f'<td class="title">{link}</td>'
                f"<td>{esc(e.get('stage'))}</td>"
                f"<td>{esc(e.get('organizer') or '—')}</td>"
                f"</tr>"
            )
        ohjelma_sections.append(f"""
<section>
  {banner}
  <table class="prog">
    <thead><tr><th>Aika</th><th>Aika-väli</th><th>Tapahtuma</th><th>Lava</th><th>Järjestäjä</th></tr></thead>
    <tbody>{"".join(prog_rows)}</tbody>
  </table>
</section>""")

        # --- Puhujat (speakers, with date column) ---
        spk_rows = []
        for e in evs:
            bg = color_of.get(e.get("start_time", ""), "#fff")
            spk_rows.append(
                f'<tr style="background:{bg}">'
                f'<td class="t">{esc(e.get("date"))}</td>'
                f'<td class="t">{esc(e.get("start_time"))}</td>'
                f'<td class="title">{esc(e.get("title"))}</td>'
                f"<td>{speakers_text(e.get('speakers'))}</td>"
                f"</tr>"
            )
        puhujat_sections.append(f"""
<section>
  {banner}
  <table class="spk">
    <thead><tr><th>Pvm</th><th>Aika</th><th>Tapahtuma</th><th>Puhujat</th></tr></thead>
    <tbody>{"".join(spk_rows)}</tbody>
  </table>
</section>""")

    total = len(events)
    sub = f"{total} tapahtumaa · 23.–26.6.2026 · Pori · värit ryhmittelevät tapahtumat alkamisajan mukaan · lähde: suomiareena.fi"

    page = _page(sub, ohjelma_sections, puhujat_sections)
    with open(INDEX_HTML, "w") as f:
        f.write(page)
    # keep the old direct URLs alive as redirects into the single page's views
    for path, view in ((OHJELMA_HTML, "ohjelma"), (PUHUJAT_HTML, "puhujat")):
        with open(path, "w") as f:
            f.write(
                '<!DOCTYPE html><html lang="fi"><head><meta charset="utf-8">'
                f'<meta http-equiv="refresh" content="0; url=index.html#{view}">'
                f"<title>SuomiAreena 2026</title></head><body>"
                f'<a href="index.html#{view}">SuomiAreena 2026</a></body></html>'
            )


if __name__ == "__main__":
    if "--build" in sys.argv:
        build()
    elif "--refresh" in sys.argv:
        crawl(refresh=True)
    else:
        crawl()
