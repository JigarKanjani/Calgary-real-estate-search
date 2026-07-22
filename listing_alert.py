#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
listing_alert.py — Calgary Resale-Listing Telegram Sender.

Fetches Calgary listings via realtor_client, deduplicates against a
committed tracker file, filters by price / property type / new-build, and
sends each new listing to Telegram directly (no LLM, no extra libraries).

"New" = "not seen before". Every run pulls the current matching listings
and only messages the ones whose MLS number isn't already in the tracker —
the same freshness mechanism the job-hunt bot uses. So running every 6
hours surfaces whatever went up (or dropped into range) since last time.

Usage: python listing_alert.py [--min 150000] [--max 600000]
"""

import os
import json
import argparse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from realtor_client import fetch_listings

WORKSPACE    = Path(__file__).parent
TRACKER_FILE = WORKSPACE / "listing-tracker-seen.md"
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

# Price window (overridable via CLI / env).
PRICE_MIN = int(os.environ.get("LISTING_PRICE_MIN", "150000"))
PRICE_MAX = int(os.environ.get("LISTING_PRICE_MAX", "600000"))

MAX_PER_RUN = 40   # safety cap on messages per run


def parse_ids(raw):
    """Split a comma-separated chat-ID string into a clean list of IDs."""
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


# Recipients. TELEGRAM_CHAT_ID_LISTINGS may hold a comma-separated list;
# falls back to the single default CHAT_ID.
CHAT_IDS = parse_ids(os.environ.get("TELEGRAM_CHAT_ID_LISTINGS")) or \
           (parse_ids(CHAT_ID) if CHAT_ID else [])

# ── Property-type allow-list ─────────────────────────────────────────────────
# Substrings matched (case-insensitive) against each listing's building type.
# Covers what you asked for: condos (apartments), townhouses/row houses,
# semi-detached, duplexes, plus regular detached houses. Set to [] to allow
# every residential type the price filter returns.
WANTED_TYPES = [
    "apartment",        # condo apartments
    "row",              # row / townhouse
    "townhouse",
    "semi",             # semi-detached
    "duplex",
    "house",            # detached
    "condo",
]

# ── New-build exclusion ──────────────────────────────────────────────────────
# You want resale ("old builds"), not builder new-construction. Realtor.ca is
# overwhelmingly resale, but a few new-construction listings slip in — drop the
# obvious ones by keyword in the type/address text.
NEW_BUILD_MARKERS = [
    "new construction", "to be built", "under construction",
    "pre-construction", "presale", "pre-sale", "builder",
]


def tg_send(text, chat_id):
    """Send a plain-text message to Telegram. Auto-trims to 4000 chars."""
    if len(text) > 4000:
        text = text[:3990] + "..."
    payload = json.dumps({
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            body = ""
        print(f"  [TG ERROR] chat_id={chat_id} HTTP {e.code}: {body or e.reason}")
        return False
    except Exception as e:
        print(f"  [TG ERROR] chat_id={chat_id} {e}")
        return False


def load_seen_mls():
    """Return set of MLS numbers already sent (from the tracker table)."""
    seen = set()
    if not TRACKER_FILE.exists():
        TRACKER_FILE.write_text(
            "| MLS | Price | Type | Address | Date | URL |\n"
            "|-----|-------|------|---------|------|-----|\n",
            encoding="utf-8",
        )
        return seen
    for line in TRACKER_FILE.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.split("|")]
        # parts[0] is empty (leading pipe); MLS lives in parts[1]
        if len(parts) >= 2 and parts[1] and parts[1] not in ("MLS", "-----"):
            seen.add(parts[1])
    return seen


def append_tracker(listing):
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = (f"| {listing['mls']} | {listing['price']} | "
           f"{listing['type'][:24]} | {listing['address'][:50]} | "
           f"{date} | {listing['url']} |\n")
    with open(TRACKER_FILE, "a", encoding="utf-8") as f:
        f.write(row)


def type_ok(listing):
    """Property-type allow-list + new-build exclusion."""
    haystack = f"{listing['type']} {listing['address']}".lower()
    if any(m in haystack for m in NEW_BUILD_MARKERS):
        return False
    if not WANTED_TYPES:
        return True
    return any(w in listing["type"].lower() for w in WANTED_TYPES)


def price_ok(listing):
    """Guard the price window client-side too (API filter can be fuzzy)."""
    p = listing.get("price_int")
    if p is None:
        return True  # keep if price couldn't be parsed rather than drop silently
    return PRICE_MIN <= p <= PRICE_MAX


def format_listing_message(listing):
    bits = []
    if listing["bedrooms"]:
        bits.append(f"{listing['bedrooms']} bed")
    if listing["bathrooms"]:
        bits.append(f"{listing['bathrooms']} bath")
    if listing["size"]:
        bits.append(listing["size"])
    spec = " · ".join(bits)

    ptype = listing["type"] or "Residential"
    found = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    msg = (
        f"🏠 New Calgary Listing — MLS# {listing['mls']}\n"
        f"📍 {listing['address'] or 'Address not listed'}\n"
        f"💰 {listing['price'] or 'Price not listed'}\n"
        f"🏢 {ptype}" + (f" · {spec}" if spec else "") + "\n"
    )
    if listing["condo_fee"]:
        msg += f"💵 Condo fee: {listing['condo_fee']}\n"
    if listing["ownership"]:
        msg += f"📄 {listing['ownership']}\n"
    msg += (
        f"📅 Found: {found}\n"
        f"\n"
        f"🔗 {listing['url']}"
    )
    return msg[:3900]


def main():
    parser = argparse.ArgumentParser(description="Calgary Listing Alert Sender")
    parser.add_argument("--min", type=int, default=PRICE_MIN, help="Min price")
    parser.add_argument("--max", type=int, default=PRICE_MAX, help="Max price")
    args = parser.parse_args()

    print(f"\n{'='*60}")
    print(f"CALGARY LISTINGS — {datetime.now().strftime('%Y-%m-%d %H:%M')} MST")
    print(f"Price window: ${args.min:,}–${args.max:,} | Recipients: {len(CHAT_IDS)}")
    print(f"{'='*60}")

    if not BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN not set — cannot send.")
    if not CHAT_IDS:
        print("[ERROR] No recipients — set TELEGRAM_CHAT_ID or "
              "TELEGRAM_CHAT_ID_LISTINGS. Continuing (dry run).")

    seen = load_seen_mls()
    print(f"Tracker: {len(seen)} MLS numbers already seen")

    # Immediate confirmation that a run kicked off.
    start_msg = (
        f"🚀 Calgary listings scan started "
        f"({datetime.now().strftime('%Y-%m-%d %H:%M')} MST)\n"
        f"💰 ${args.min:,}–${args.max:,} · condos / townhouses / "
        f"semi-detached / houses (resale)\n"
        f"⏳ New listings will land here shortly."
    )
    for cid in CHAT_IDS:
        tg_send(start_msg, cid)

    listings = fetch_listings(args.min, args.max)

    sent = 0
    for listing in listings:
        if sent >= MAX_PER_RUN:
            break
        if not listing["mls"] or not listing["url"]:
            continue
        if listing["mls"] in seen:
            continue
        if not price_ok(listing):
            continue
        if not type_ok(listing):
            continue

        msg = format_listing_message(listing)
        print(f"  -> {listing['mls']} | {listing['price']} | {listing['address']}")

        delivered = False
        for cid in CHAT_IDS:
            if tg_send(msg, cid):
                delivered = True

        # Mark as seen once we've fetched it, even in a dry run (no recipients),
        # so the tracker reflects what was surfaced. Only append when it's a new
        # MLS we've actually processed.
        seen.add(listing["mls"])
        append_tracker(listing)
        if delivered or not CHAT_IDS:
            sent += 1

    summary_msg = (
        f"✅ Calgary listings scan done — "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} MST\n"
        f"{sent} new listing(s) · window ${args.min:,}–${args.max:,}\n"
        f"Source: Realtor.ca"
    )
    print(f"\n{summary_msg}")
    for cid in CHAT_IDS:
        tg_send(summary_msg, cid)

    print(f"{'='*60}\nDONE — {sent} new listings")


if __name__ == "__main__":
    main()
