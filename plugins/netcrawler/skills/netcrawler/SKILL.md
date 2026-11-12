---
name: netcrawler
description: "Crawl sites, clear gates, search keyless with netcrawler."
version: 1.0.0
author: Fox, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Crawler, Scraping, Anti-Bot, Search, Corpus, Research]
    related_skills: [scrapling, blocked-page-recovery, grounded-citations]
---

# NetCrawler

A six-tool internet layer: keyless federated search, a single-page fetcher that
escalates from fast HTTP to a stealth browser and clears age/consent gates, a
site crawler with sitemaps and adaptive stopping, CSS/XPath structured
extraction, and a local SQLite+FTS5 corpus that survives across sessions.

Every tool returns JSON with `ok`. On failure read `error.code`, `blocked`, and
`attempts` — `attempts` says exactly which rung was tried and why it failed.

## When to Use

- The user gives a link and a plain fetch fails, or the page shows an age gate,
  cookie wall, or "verify you are human" interstitial → `net_fetch`.
- You need page *content*, not just links, for a research question →
  `net_search` (it searches several engines and reads the top hits).
- One engine keeps returning nothing, or results must be cross-checked →
  `net_search` with `engines` or `vertical`.
- The user asks about a whole site, docs tree, blog, or changelog →
  `net_crawl`, then `net_corpus`.
- You need fields out of a listing/detail page (prices, titles, dates) →
  `net_extract`.
- A previous session crawled something and you need it again → `net_corpus`.

Don't use for: a single URL that `web_extract` already returns cleanly (that is
cheaper), or for anything requiring a login you do not have.

## Tools

| Tool | Use |
|---|---|
| `net_search` | `query`, `limit`, `fetch`, `read_count`, `vertical` (web/news/reference/code/papers/discussions), `engines`, `site` |
| `net_fetch` | `url`, `mode` (auto/http/browser), `headless`, `save`, `query`, `max_chars` |
| `net_crawl` | `url`, `max_pages`, `max_depth`, `strategy` (bfs/dfs/best-first), `query`, `include`, `exclude`, `wait_s` |
| `net_status` | `job_id`, `action` (status/list/cancel) |
| `net_extract` | `url`/`doc_id`, `spec` = `{item, fields}`, `format` (json/csv) |
| `net_corpus` | `action` (search/get/sites/stats/export), `query`, `site`, `doc_id` |

## Procedure

1. **Single page.** `net_fetch(url)`. If `ok` is false, read `attempts`: an
   `http` entry with `blocked: "challenge-page"` means the site fights clients —
   retry with `mode="browser"`. A `no-snapshot` archive entry means the page is
   genuinely unavailable.
2. **Research question.** `net_search(query, read_count=3)`. Check
   `read_summary.readable` — if 0, the hits were redirect stubs; rerun with
   `vertical="news"` or different `engines`.
3. **Whole site.** `net_crawl(url, query="<what you want>", max_pages=60,
   max_depth=2)`. It returns a `job_id` immediately. Poll `net_status(job_id)`
   until `status` is `done`, then `net_corpus(action="search", query=..., site=host)`.
   Stop polling after a few checks — the crawl keeps running in the background.
4. **Structured data.** Inspect one page first (`net_fetch`), then
   `net_extract(url, spec={"item": "<container selector>", "fields": {...}})`.
   Use `{"selector": ..., "attr": "href"}` for attributes and `{"all": true}`
   for lists.
5. **Reuse.** Before fetching a site again, try `net_corpus(action="sites")` and
   `net_corpus(action="search")` — a previous crawl may already hold it.

## Choosing parameters

- `strategy="best-first"` + `query` for "find the pages about X"; `bfs` for
  "mirror this site"; `dfs` for docs trees.
- `adaptive=true` (default) stops when pages stop introducing new query terms.
- `respect_robots` defaults to true. Only set it false when the user explicitly
  asks; `crawl-delay` is always honoured.
- `max_pages` is the real budget guard. A 500-page crawl at default politeness
  takes many minutes — prefer 40-80 pages with a `query` and let best-first
  find the relevant material.

## Pitfalls

- **Do not poll `net_status` in a tight loop.** Sleep between checks (or do
  other work); each call hits SQLite.
- **`net_search` results can legitimately be empty.** Every free engine
  rate-limits; the `engines` map tells you which ones were `blocked-or-empty`
  versus `cooling-down`.
- **`text` in `net_corpus` hits is a snippet.** Use `action="get"` for full
  markdown.
- **A snapshot is not live content.** If `provenance` is `snapshot`, say so when
  reporting facts, and give the snapshot date.
- **PDF and JS-heavy pages come through `net_fetch`**, not `web_extract`.
- **Corpus is local and private.** Never paste its contents into an external
  service unless the user asked.

## Verification

- `net_fetch` on a page you know is gated: expect `method: "browser"` and a
  non-empty `gates_clicked` when a gate was dismissed.
- `net_crawl` then `net_status`: `pages` should rise, `queued` should fall, and
  `stopped` should read `frontier-empty` or `page-cap`.
- `net_corpus(action="stats")` shows document/word counts; `action="search"`
  must return the pages the crawl just stored.
- `net_extract` with a wrong selector returns `count: 0` rather than an error —
  fix the selector against `net_fetch` output.