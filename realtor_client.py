#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
realtor_client.py — Calgary resale-listing fetcher.

Talks directly to Realtor.ca's internal property-search endpoint
(PropertySearch_Post) and returns a list of normalized listing dicts.

This is the ONLY source-specific file. Everything downstream (dedup,
filtering, Telegram formatting) consumes the normalized dicts, so a
different data source (a licensed CREA/DDF feed, etc.) can be dropped in by
re-implementing `fetch_listings()` with the same return shape.

BOT PROTECTION: Realtor.ca sits behind Imperva/Incapsula. From a fresh
residential IP the endpoint usually answers with JSON; from a datacenter IP
(e.g. a GitHub Actions runner) it returns an HTML "Access Denied" page.
To get past it, pick ONE of these (checked in this order):

  1. Managed scraper (recommended for GitHub Actions) — set an API key and
     the request is routed through a residential-proxy scraping API that
     clears Imperva for you. Supported providers (auto-selected by which
     credential is present):
        • ScrapingBee : set  SCRAPINGBEE_API_KEY
        • Scrape.do   : set  SCRAPEDO_TOKEN
     Force one explicitly with  SCRAPER_PROVIDER=scrapingbee|scrapedo|direct.
  2. Generic proxy — set REALTOR_PROXY to "http://user:pass@host:port".
  3. Direct — no config; works only from an unblocked (residential) IP.
"""

import os
import re
import json
import time
import urllib.parse
import urllib.request
import urllib.error

# ── Realtor.ca internal search endpoint ──────────────────────────────────────
REALTOR_API   = "https://api2.realtor.ca/Listing.svc/PropertySearch_Post"
LISTING_BASE  = "https://www.realtor.ca"

# Optional generic proxy for clearing datacenter-IP blocks. Format understood
# by urllib, e.g. "http://user:pass@host:port". Empty = direct connection.
REALTOR_PROXY = os.environ.get("REALTOR_PROXY", "").strip()

# ── Managed scraper config ───────────────────────────────────────────────────
# A managed scraper fetches the target through a residential-proxy API that
# clears Imperva, so the free GitHub Actions runner doesn't get blocked.
SCRAPINGBEE_API_KEY = os.environ.get("SCRAPINGBEE_API_KEY", "").strip()
SCRAPEDO_TOKEN      = os.environ.get("SCRAPEDO_TOKEN", "").strip()
# "scrapingbee" | "scrapedo" | "direct" — overrides auto-detection when set.
SCRAPER_PROVIDER    = os.environ.get("SCRAPER_PROVIDER", "").strip().lower()
# ISO country the scraper should proxy from (Realtor.ca is geo-sensitive).
SCRAPER_COUNTRY     = os.environ.get("SCRAPER_COUNTRY", "ca").strip().lower()

SCRAPINGBEE_ENDPOINT = "https://app.scrapingbee.com/api/v1/"
SCRAPEDO_ENDPOINT    = "https://api.scrape.do/"


def _active_provider():
    """Return the managed-scraper provider to use, or "" for a direct call."""
    if SCRAPER_PROVIDER:
        return "" if SCRAPER_PROVIDER == "direct" else SCRAPER_PROVIDER
    if SCRAPINGBEE_API_KEY:
        return "scrapingbee"
    if SCRAPEDO_TOKEN:
        return "scrapedo"
    return ""

# ── Calgary bounding box (city of Calgary, approx.) ──────────────────────────
# Realtor.ca searches by a lat/long rectangle, not a city name.
CALGARY_BBOX = {
    "LatitudeMin":  50.842,
    "LatitudeMax":  51.213,
    "LongitudeMin": -114.316,
    "LongitudeMax": -113.859,
}

# Browser-like headers — the endpoint rejects obvious non-browser clients.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-CA,en;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Origin": "https://www.realtor.ca",
    "Referer": "https://www.realtor.ca/map",
}


def _opener(use_proxy):
    """Build a urllib opener; apply REALTOR_PROXY only on a direct call."""
    handlers = []
    if use_proxy and REALTOR_PROXY:
        handlers.append(urllib.request.ProxyHandler(
            {"http": REALTOR_PROXY, "https": REALTOR_PROXY}))
    return urllib.request.build_opener(*handlers)


def _build_request(data):
    """Build the (Request, use_proxy, label) for the active provider.

    The target Realtor.ca POST (form body + browser headers) is preserved in
    every case; a managed scraper just wraps it and forwards it through a
    residential proxy that clears Imperva.
    """
    provider = _active_provider()

    if provider == "scrapingbee":
        params = {
            "api_key": SCRAPINGBEE_API_KEY,
            "url": REALTOR_API,
            "premium_proxy": "true",       # residential IPs — needed for Imperva
            "country_code": SCRAPER_COUNTRY,
            "render_js": "false",          # it's a JSON API, no browser needed
            "forward_headers": "true",     # pass our Spb-* headers to the target
        }
        api_url = SCRAPINGBEE_ENDPOINT + "?" + urllib.parse.urlencode(params)
        # ScrapingBee forwards headers prefixed with "Spb-" to the target.
        headers = {"Spb-" + k: v for k, v in HEADERS.items()}
        headers["Content-Type"] = HEADERS["Content-Type"]  # for the POST body
        req = urllib.request.Request(api_url, data=data, headers=headers,
                                     method="POST")
        return req, False, "ScrapingBee"

    if provider == "scrapedo":
        params = {
            "token": SCRAPEDO_TOKEN,
            "url": REALTOR_API,
            "super": "true",               # residential proxy ("super" mode)
            "geoCode": SCRAPER_COUNTRY,
            "customHeaders": "true",       # forward our headers as-is
        }
        api_url = SCRAPEDO_ENDPOINT + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(api_url, data=data, headers=dict(HEADERS),
                                     method="POST")
        return req, False, "Scrape.do"

    # Direct (optionally through REALTOR_PROXY).
    req = urllib.request.Request(REALTOR_API, data=data, headers=dict(HEADERS),
                                 method="POST")
    return req, True, "direct"


def _post(payload, timeout=40):
    """POST form-encoded payload to Realtor.ca, return parsed JSON or None."""
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req, use_proxy, label = _build_request(data)
    try:
        with _opener(use_proxy).open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        # Imperva/Incapsula answers with 403 + an HTML block page from
        # datacenter IPs. Make the remedy obvious in the logs.
        if e.code in (403, 429) and "<html" in body.lower():
            print(f"  [Realtor BLOCKED via {label}] HTTP {e.code} bot-protection "
                  f"page. Configure a managed scraper (SCRAPINGBEE_API_KEY or "
                  f"SCRAPEDO_TOKEN) or a residential REALTOR_PROXY.")
        else:
            print(f"  [Realtor HTTP {e.code} via {label}] {e.reason} {body[:200]}")
        return None
    except Exception as e:
        print(f"  [Realtor ERROR via {label}] {e}")
        return None

    # A bot-block page is HTML, not JSON — detect and report clearly.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        snippet = raw.strip()[:120].replace("\n", " ")
        print(f"  [Realtor BLOCKED via {label}] Non-JSON response (likely "
              f"Imperva, or a scraper error). Got: {snippet!r}")
        return None


def _price_to_int(price_str):
    """'$445,000' -> 445000 ; returns None if unparseable."""
    if not price_str:
        return None
    digits = "".join(c for c in str(price_str) if c.isdigit())
    return int(digits) if digits else None


# .NET ticks at the Unix epoch (1970-01-01). Realtor.ca's InsertedDateUTC is a
# .NET ticks value (100-nanosecond intervals since 0001-01-01), e.g.
# "638724324981000000".
_TICKS_AT_UNIX_EPOCH = 621355968000000000


def _parse_size_sqft(size_str):
    """'1,200 sqft' -> 1200.0 ; handles ranges, m² conversion. None if absent."""
    if not size_str:
        return None
    s = str(size_str).lower().replace(",", "")
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", s)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return None
    val = sum(nums) / len(nums) if len(nums) > 1 else nums[0]   # avg a range
    if "m2" in s or "sqm" in s or "square met" in s or "m²" in s:
        val *= 10.7639   # m² -> sqft
    return val if val > 0 else None


def _parse_inserted_date(val):
    """Parse Realtor.ca InsertedDateUTC into a Unix epoch (seconds), or None.

    Handles the .NET ticks form (the common case), the "/Date(ms)/" form, and
    a plain epoch-seconds/ms integer as a fallback.
    """
    if not val:
        return None
    s = str(val).strip()
    # "/Date(1699999999000)/" or "/Date(1699999999000-0000)/"
    m = re.search(r"/Date\((\d+)", s)
    if m:
        return int(m.group(1)) / 1000.0
    if s.isdigit():
        n = int(s)
        if n >= 100000000000000000:      # ~18 digits => .NET ticks
            return (n - _TICKS_AT_UNIX_EPOCH) / 10_000_000.0
        if n >= 10000000000000:          # ~13 digits => epoch ms
            return n / 1000.0
        if n >= 1000000000:              # ~10 digits => epoch seconds
            return float(n)
    return None


def _normalize(result):
    """Map one Realtor.ca search result to our normalized listing dict."""
    prop     = result.get("Property", {}) or {}
    building = result.get("Building", {}) or {}
    address  = prop.get("Address", {}) or {}

    rel = result.get("RelativeDetailsURL", "") or ""
    url = (LISTING_BASE + rel) if rel.startswith("/") else rel

    # Condo / maintenance fee: not always present in the search payload.
    # Try the keys that sometimes carry it; otherwise flag "see listing".
    condo_fee = (prop.get("CondoFee")
                 or prop.get("MaintenanceFee")
                 or result.get("MaintenanceFee"))

    # Listing recency. InsertedDateUTC (.NET ticks) is the authoritative "when
    # was this listed" signal; TimeOnRealtor ("5 hours ago") is a human string.
    inserted = (result.get("InsertedDateUTC")
                or prop.get("InsertedDateUTC"))

    price_int = _price_to_int(prop.get("Price", ""))
    size_sqft = _parse_size_sqft(building.get("SizeInterior"))
    # Value metric: price per interior square foot (lower = better value).
    pps = round(price_int / size_sqft, 2) if (price_int and size_sqft) else None

    return {
        "mls":        str(result.get("MlsNumber", "")).strip(),
        "price":      prop.get("Price", ""),
        "price_int":  price_int,
        "address":    address.get("AddressText", "").replace("|", ", ").strip(),
        "type":       (building.get("Type") or prop.get("Type") or "").strip(),
        "bedrooms":   (building.get("Bedrooms") or "").strip(),
        "bathrooms":  (building.get("BathroomTotal") or "").strip(),
        "size":       (building.get("SizeInterior") or "").strip(),
        "size_sqft":  size_sqft,
        "price_per_sqft":  pps,
        "condo_fee":  str(condo_fee).strip() if condo_fee else "",
        "ownership":  (prop.get("OwnershipType") or "").strip(),
        "listed_epoch":    _parse_inserted_date(inserted),
        "time_on_realtor": str(result.get("TimeOnRealtor") or "").strip(),
        "url":        url,
    }


def fetch_listings(price_min, price_max, bbox=None, records_per_page=200,
                   max_pages=10, transaction="sale", pause=1.0):
    """Fetch all Calgary resale listings in [price_min, price_max].

    Returns a list of normalized listing dicts (see `_normalize`).
    Pagination stops when a page returns no results or max_pages is hit.
    """
    bbox = bbox or CALGARY_BBOX
    transaction_id = 2 if transaction == "sale" else 1  # 2 = For Sale

    provider = _active_provider() or ("proxy" if REALTOR_PROXY else "direct")
    print(f"  [Realtor] fetch mode: {provider}")

    listings = []
    seen_mls = set()
    for page in range(1, max_pages + 1):
        payload = {
            **bbox,
            "PriceMin": price_min,
            "PriceMax": price_max,
            "TransactionTypeId": transaction_id,
            "PropertySearchTypeId": 1,   # 1 = Residential
            "Currency": "CAD",
            "RecordsPerPage": records_per_page,
            "ApplicationId": 1,
            "CultureId": 1,
            "Version": "7.0",
            # 6-D = newest first (known-good). We fetch the whole market for the
            # value ranking anyway and re-sort client-side, so this only sets
            # fetch order. Override via REALTOR_SORT to experiment server-side.
            "Sort": os.environ.get("REALTOR_SORT", "6-D"),
            "CurrentPage": page,
        }
        print(f"  [Realtor] page {page} (price {price_min:,}-{price_max:,})...")
        data = _post(payload)
        if not data:
            break

        results = data.get("Results", []) or []
        if not results:
            break

        for r in results:
            listing = _normalize(r)
            if listing["mls"] and listing["mls"] not in seen_mls:
                seen_mls.add(listing["mls"])
                listings.append(listing)

        # Realtor.ca reports total pages in Paging; stop when we've read them.
        paging = data.get("Paging", {}) or {}
        total_pages = paging.get("TotalPages")
        if total_pages and page >= total_pages:
            break

        time.sleep(pause)  # be polite between pages

    print(f"  [Realtor] {len(listings)} unique listings fetched")
    return listings


if __name__ == "__main__":
    # Quick manual smoke test.
    got = fetch_listings(150000, 600000, max_pages=1)
    for l in got[:5]:
        print(f"  {l['mls']} | {l['price']} | {l['type']} | {l['address']}")
    print(f"Total: {len(got)}")
