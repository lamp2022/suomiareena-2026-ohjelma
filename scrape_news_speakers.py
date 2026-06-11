#!/usr/bin/env python3
"""SuomiAreena 2026 ajankohtaista (news) speaker scraper.

The ohjelma event pages carry structured <ul class="speaker-list"> markup
(handled by scrape.py). News posts under /ajankohtaista/ do NOT: speakers are
named inline in prose, each wrapped in <strong>Name</strong> followed by a
comma-separated descriptor ("..., kansanedustaja (sd.)"). Some of those people
never get an ohjelma event page, so they are invisible to scrape.py.

This tool walks the paginated ajankohtaista index, fetches each 2026 post, and
pulls <strong> person names + the descriptor text that trails each one. The
build step deduplicates across posts, cross-references ohjelma (events.json)
with Finnish-inflection-aware matching, and tags every name:
  new            -> not in ohjelma, treat as a fresh speaker
  in_ohjelma     -> already covered by scrape.py (often a declined form)
  probable_byline-> appears in >=BYLINE_MIN posts, likely a press contact

Checkpointed per the long-run rule: one JSONL line per post URL completed,
resumable on restart.

Usage:
  python3 scrape_news_speakers.py            # crawl new posts, then build
  python3 scrape_news_speakers.py --refresh  # re-fetch index + all posts
  python3 scrape_news_speakers.py --build     # rebuild json from checkpoint
"""

import html
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_URL = "https://www.suomiareena.fi/ajankohtaista/"
CHECKPOINT = os.path.join(HERE, "news_speakers.jsonl")
FINAL_JSON = os.path.join(HERE, "news_speakers.json")
OHJELMA_JSON = os.path.join(HERE, "events.json")
UA = "Mozilla/5.0"

BYLINE_MIN = 4  # a name in >= this many posts is treated as a press contact
MAX_INDEX_PAGES = 20


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


# --- index → post URLs ------------------------------------------------------

POST_RE = re.compile(r'href="(https://www\.suomiareena\.fi/(20\d\d)/\d\d/[^"]+)"')


def discover_posts():
    """Walk paginated ajankohtaista index, return 2026 post URLs (ordered)."""
    seen = []
    seen_set = set()
    for p in range(1, MAX_INDEX_PAGES + 1):
        url = INDEX_URL if p == 1 else f"{INDEX_URL}page/{p}/"
        try:
            h = fetch(url)
        except Exception:
            break  # 404 past last page
        page_urls = [
            (m.group(1).rstrip("/") + "/", m.group(2)) for m in POST_RE.finditer(h)
        ]
        fresh = [u for u, _ in page_urls if u not in seen_set]
        if not fresh and p > 1:
            break
        for u, year in page_urls:
            if year == "2026" and u not in seen_set:
                seen_set.add(u)
                seen.append(u)
        time.sleep(0.3)
    return seen


# --- post → speakers --------------------------------------------------------

CONTENT_RE = re.compile(
    r'<div class="entry-content">(.*?)</div><!-- \.entry-content -->', re.S
)
STRONG_RE = re.compile(r"<strong>(.*?)</strong>(.*?)(?=<strong>|</p>|</li>|$)", re.S)
_HEADING_WORDS = (
    "klo",
    "ohjelma",
    "lava",
    "keskustel",
    "suomiareena",
    "päivä",
    "teema",
)


def looks_like_name(t):
    t = t.strip().rstrip(".,:;")
    words = t.split()
    if not (1 < len(words) <= 4):
        return False
    low = t.lower()
    if any(w in low for w in _HEADING_WORDS):
        return False
    # every word starts uppercase (or is a non-alpha token like a nickname quote)
    return all((w[:1].isupper() or not w[:1].isalpha()) for w in words)


def parse_post(page):
    """Return list of {name, descriptor} from a news post body."""
    m = CONTENT_RE.search(page)
    block = m.group(1) if m else page
    out = []
    for sm in STRONG_RE.finditer(block):
        name = clean(sm.group(1)).rstrip(".,:;")
        if not looks_like_name(name):
            continue
        # descriptor = comma-led text trailing the name, up to next strong/para
        desc = clean(sm.group(2)).lstrip(",").strip()
        # keep only the first clause-ish chunk (stop at sentence end)
        desc = re.split(r"(?<=[a-zäö])\.\s", desc)[0].strip()
        out.append({"name": name, "descriptor": desc[:200]})
    return out


def slug(url):
    return url.rstrip("/").split("/")[-1]


# --- checkpoint -------------------------------------------------------------


def _load_jsonl(path, key):
    """Latest record per `key` from a JSONL checkpoint (latest line wins)."""
    done = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                done[rec[key]] = rec
    return done


def load_done():
    return _load_jsonl(CHECKPOINT, "url")


def crawl(refresh=False):
    posts = discover_posts()
    print(f"discovered {len(posts)} 2026 posts on ajankohtaista index")
    done = load_done()
    print(f"checkpoint has {len(done)} done")
    force = set(done) if refresh else set()

    with open(CHECKPOINT, "a") as ckpt:
        for i, url in enumerate(posts, 1):
            if url in done and url not in force:
                continue
            rec = {"url": url, "slug": slug(url)}
            try:
                rec["speakers"] = parse_post(fetch(url))
                rec["error"] = None
            except Exception as e:  # no silent errors
                rec["speakers"] = []
                rec["error"] = f"{type(e).__name__}: {e}"
                print(f"  ! {slug(url)} -> {rec['error']}", file=sys.stderr)
            ckpt.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ckpt.flush()
            done[url] = rec
            if i % 10 == 0 or i == len(posts):
                print(f"{i}/{len(posts)} scanned")
            time.sleep(0.4)
    print("crawl complete")
    build()


# --- build: dedup + ohjelma cross-ref --------------------------------------


def _norm(s):
    return re.sub(r"[^\wäöå]", "", s.lower())


def _word_matches(a, b):
    """One word is a stem-prefix of the other (Finnish inflection tolerance)."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b:
        return True
    lo, hi = sorted((a, b), key=len)
    return len(lo) >= 3 and hi.startswith(lo)


def same_person(n1, n2):
    """First name exact-ish, remaining words stem-match. Handles declension."""
    w1, w2 = n1.split(), n2.split()
    if not w1 or not w2 or len(w1) != len(w2):
        return False
    return all(_word_matches(a, b) for a, b in zip(w1, w2))


def ohjelma_names():
    if not os.path.exists(OHJELMA_JSON):
        return []
    d = json.load(open(OHJELMA_JSON))
    names = []
    for e in d:
        for s in e.get("speakers") or []:
            n = s.get("name") if isinstance(s, dict) else s
            if n:
                names.append(n)
    return sorted(set(names))


def build():
    done = load_done()
    oh = ohjelma_names()
    oh_lc = {n.lower() for n in oh}

    # aggregate: name -> {descriptor, posts:set}
    agg = {}
    for rec in done.values():
        for sp in rec.get("speakers") or []:
            name = sp["name"]
            entry = agg.setdefault(name, {"descriptor": "", "posts": set()})
            entry["posts"].add(rec["slug"])
            if sp.get("descriptor") and not entry["descriptor"]:
                entry["descriptor"] = sp["descriptor"]

    out = []
    for name, e in sorted(agg.items()):
        posts = sorted(e["posts"])
        if name.lower() in oh_lc:
            status, match = "in_ohjelma", name
        else:
            match = next((o for o in oh if same_person(name, o)), None)
            if match:
                status = "in_ohjelma"
            elif len(posts) >= BYLINE_MIN:
                status = "probable_byline"
            else:
                status = "new"
        out.append(
            {
                "name": name,
                "descriptor": e["descriptor"],
                "posts": posts,
                "status": status,
                "ohjelma_match": match if (match and match != name) else None,
            }
        )

    json.dump(out, open(FINAL_JSON, "w"), ensure_ascii=False, indent=1)
    counts = {}
    for r in out:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print(f"wrote {FINAL_JSON} ({len(out)} names) {counts}")
    new = [r for r in out if r["status"] == "new"]
    print(f"\n=== {len(new)} NEW speakers (not in ohjelma) ===")
    for r in new:
        d = f"  — {r['descriptor']}" if r["descriptor"] else ""
        print(f"  {r['name']}{d}  {r['posts']}")


# --- apply: merge curated news speakers into ohjelma events.jsonl -----------

OHJELMA_CKPT = os.path.join(HERE, "events.jsonl")
CURATION = os.path.join(HERE, "news_curation.json")


def _ohjelma_load_done():
    """Latest record per post_id from the ohjelma checkpoint."""
    return _load_jsonl(OHJELMA_CKPT, "post_id")


def apply_curation():
    """Merge news_curation.json speakers into the matching ohjelma events,
    append augmented records to events.jsonl (latest-wins), then rebuild."""
    cur = {k: v for k, v in json.load(open(CURATION)).items() if not k.startswith("_")}
    done = _ohjelma_load_done()
    appended = []
    with open(OHJELMA_CKPT, "a") as ckpt:
        for pid, spec in cur.items():
            rec = done.get(pid)
            if not rec:
                print(
                    f"  ! post_id {pid} ({spec['event']}) not in events.jsonl; skip",
                    file=sys.stderr,
                )
                continue
            existing = list(rec.get("speakers") or [])
            have_norm = [_norm(s.get("name", "")) for s in existing]
            added = 0
            for sp in spec["speakers"]:
                if any(same_person(sp["name"], e.get("name", "")) for e in existing):
                    continue
                if _norm(sp["name"]) in have_norm:
                    continue
                existing.append({**sp, "source": "news"})
                added += 1
            rec = dict(rec)
            rec["speakers"] = existing
            ckpt.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ckpt.flush()
            appended.append((spec["event"], added, len(existing)))
    for ev, added, total in appended:
        print(f"  {ev}: +{added} news speakers (now {total})")
    # rebuild ohjelma site from augmented checkpoint
    import subprocess

    subprocess.run(
        [sys.executable, os.path.join(HERE, "scrape.py"), "--build"], check=True
    )


if __name__ == "__main__":
    if "--apply" in sys.argv:
        apply_curation()
    elif "--build" in sys.argv:
        build()
    elif "--refresh" in sys.argv:
        crawl(refresh=True)
    else:
        crawl()
