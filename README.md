# Calgary Real Estate Search — Telegram Bot

A self-contained bot that scans **Realtor.ca** every 6 hours for new **resale**
Calgary property listings in a price window and pushes each new one to Telegram.
Mirrors the mechanism of a job-alert bot, but for real estate.

- **Price window:** $150,000 – $600,000 (configurable)
- **Property types:** condos (apartments), townhouses / row, semi-detached,
  duplexes, detached houses — resale only (new-construction excluded)
- **Area:** City of Calgary
- **Freshness:** "new" = not seen before, tracked by MLS number in a committed file

---

## How it works

```
GitHub Actions (cron, every 6h)
        │
        ▼
listing_alert.py ──► realtor_client.py ──► Realtor.ca PropertySearch_Post
        │                                        (listings in bbox + price)
        │
        ├─ load seen MLS numbers  (listing-tracker-seen.md)
        ├─ filter: price window · property type · exclude new-builds
        ├─ send NEW listings      ──► Telegram sendMessage
        └─ append new MLS to tracker, commit back to repo
```

| File | Role |
|---|---|
| `realtor_client.py` | **Source adapter.** Fetches + normalizes listings. The only source-specific file — swap it for a managed scraper / CREA DDF feed and everything downstream is unchanged. |
| `listing_alert.py` | **Orchestrator.** Dedup against the tracker, filter, format, send to Telegram. |
| `listing-tracker-seen.md` | **Memory.** Committed table of every MLS number ever sent; makes "new" work across stateless runs. |
| `.github/workflows/calgary-listings.yml` | **Scheduler.** 6-hour cron + manual trigger; commits the tracker back. |

The dedup-via-committed-file trick is what makes "only new listings" work on
stateless GitHub runners: the tracker is the persistent state.

---

## Setup

1. Add Telegram secrets — see [`RECIPIENTS.md`](./RECIPIENTS.md).
   At minimum set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID_LISTINGS`.
2. (Optional) set `REALTOR_PROXY` if Realtor.ca blocks the runner IP.
3. Enable Actions and either wait for the 6-hour cron or run the
   **Calgary Listings** workflow manually from the Actions tab.

## Run locally

```bash
export TELEGRAM_BOT_TOKEN=...          # optional for a dry run
export TELEGRAM_CHAT_ID_LISTINGS=...   # optional for a dry run
python listing_alert.py --min 150000 --max 600000
```

With no recipients set it does a **dry run**: fetches and prints matches and
populates the tracker without sending Telegram messages.

Test just the fetch layer:

```bash
python realtor_client.py
```

---

## Tuning

| What | Where |
|---|---|
| Price window | `--min/--max`, or `LISTING_PRICE_MIN/MAX` env (set in the workflow) |
| Property types | `WANTED_TYPES` in `listing_alert.py` (empty list = all residential) |
| New-build exclusion | `NEW_BUILD_MARKERS` in `listing_alert.py` |
| Search area | `CALGARY_BBOX` in `realtor_client.py` (lat/long rectangle) |
| Cron cadence | `.github/workflows/calgary-listings.yml` |

---

## Data-source note

Realtor.ca (CREA) has no free public API and sits behind Imperva/Incapsula bot
protection. This bot calls the site's internal `PropertySearch_Post` endpoint
directly, which works from most residential IPs but **may be blocked from
GitHub's datacenter IPs** (HTTP 403 + a block page). If you see
`[Realtor BLOCKED]` in the logs:

1. Set `REALTOR_PROXY` to a residential/rotating proxy, **or**
2. Replace `realtor_client.py`'s `fetch_listings()` with a managed scraper
   (Apify Realtor.ca actor, ScrapingBee, Scrape.do) or a licensed CREA DDF
   feed — the return shape is documented in `_normalize()`, so nothing else
   needs to change.

Condo/maintenance fees are not always present in the search payload; when
absent the message omits the line (the exact fee is on the listing page).
