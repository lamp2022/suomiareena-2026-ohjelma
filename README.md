# SuomiAreena 2026 – Ohjelma

Scraped programme of [SuomiAreena 2026](https://www.suomiareena.fi/suomiareena-2026/ohjelma/) (Pori, 23.–26.6.2026), rendered as two browsable tables.

**Live page** (GitHub Pages): one page, `index.html`, with an **Ohjelma / Puhujat** toggle.
- **Ohjelma** — programme, colour-grouped by start time (`#ohjelma`)
- **Puhujat** — speakers per event, with date (`#puhujat`)

`ohjelma.html` and `puhujat.html` are kept as redirects into the toggle's two views, so old links still work.

215 events across four days. Rows are coloured by start-time slot: every event sharing a start time gets the same colour, the next slot a different one. A sticky day banner keeps the date visible while scrolling. Speakers not yet published by the organiser show **"ei vielä tiedossa"**. Each event's full description is stored in `events.json` (not shown in the tables).

## Files

| File | What |
|------|------|
| `scrape.py` | Scraper + page builder |
| `events.json` | Final dataset (events incl. description + speakers) |
| `events.jsonl` | Append-only crawl checkpoint (resumable) |
| `index.html` | The rendered page (Ohjelma/Puhujat toggle) |
| `ohjelma.html` / `puhujat.html` | Redirects into `index.html`'s two views |
| `ohjelma_raw.html` | Saved snapshot of the source list page |

## Run

```bash
python3 scrape.py            # parse list page + fetch each event's detail (checkpointed, skip-done)
python3 scrape.py --refresh  # re-download the list + refetch events still missing speakers, then rebuild
python3 scrape.py --build    # rebuild events.json + the two HTML pages from the checkpoint (no refetch)
```

`--refresh` is the one to run later, once the organiser has published more speakers: it re-fetches every event currently showing **"ei vielä tiedossa"** (and any that errored), picks up newly added events, and rewrites both pages. To force a full fresh crawl instead, delete `events.jsonl` first.

## Scraping approach

Principles mirrored from a private property-scraper (`kohdescraper`):

- **HTTP fetch + HTML parse** — the source is server-rendered WordPress, so no headless browser is needed.
- **Checkpointed, resumable batches** — every event detail is appended to `events.jsonl`; already-fetched ids are skipped on restart.
- **Polite jittered crawl** — short randomised delay between detail fetches.
- **Normalizer → one canonical shape** — heterogeneous markup mapped to a single event record.
- **No silent errors** — a failed fetch is recorded with its reason, never swallowed.
- **Conditional-required fields** — a field absent in the source is empty, not a failure.

Data © SuomiAreena. This repo is an unofficial, read-only rendering.
