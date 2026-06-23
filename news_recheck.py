#!/usr/bin/env python3
"""Report which ajankohtaista posts changed since the last LLM read.

The expensive step in vetting the news (ajankohtaista) speakers is reading each
post and judging who is a real festival speaker — that was done once by an agent
and the verdict cached in news_parse_cache.json. This script re-fingerprints the
current crawl (run scrape_news_speakers.py first) and prints only the posts whose
content changed, so the agent re-reads ONLY those instead of all 24 every time.

    python3 scrape_news_speakers.py   # refresh crawl (cheap HTTP, checkpointed)
    python3 news_recheck.py           # -> list of posts needing a fresh read

Exit 0 + "no changes" means the cached verdict still holds; skip the re-read.
"""

import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CRAWL = os.path.join(HERE, "news_speakers.jsonl")
CACHE = os.path.join(HERE, "news_parse_cache.json")


def fingerprint(speakers):
    """Stable hash of the post's <strong> speaker names — a content change
    (added/removed panelist) flips it; cosmetic edits do not."""
    norm = sorted(
        (s.get("name", "") if isinstance(s, dict) else s).strip()
        for s in (speakers or [])
    )
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False).encode()).hexdigest()[
        :16
    ]


def main():
    if not os.path.exists(CRAWL):
        print(f"missing {CRAWL}; run scrape_news_speakers.py first", file=sys.stderr)
        sys.exit(1)
    cached = {}
    if os.path.exists(CACHE):
        cached = json.load(open(CACHE)).get("posts", {})

    current = {}
    for line in open(CRAWL):
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        current[e["url"]] = fingerprint(e.get("speakers"))

    new = [u for u in current if u not in cached]
    changed = [
        u for u in current if u in cached and cached[u]["fingerprint"] != current[u]
    ]
    gone = [u for u in cached if u not in current]

    if not new and not changed:
        print(f"no changes — all {len(current)} posts match cache; skip re-read")
        return
    if new:
        print(f"NEW posts to read ({len(new)}):")
        for u in new:
            print("  " + u)
    if changed:
        print(f"CHANGED posts to re-read ({len(changed)}):")
        for u in changed:
            print("  " + u)
    if gone:
        print(f"(note: {len(gone)} cached posts no longer on index)")


if __name__ == "__main__":
    main()
