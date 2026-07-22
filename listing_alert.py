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
import re
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

# Freshness cap: only surface listings put up within the last N hours.
# 0 disables the age filter (falls back to dedup-only "new since last seen").
MAX_AGE_HOURS = float(os.environ.get("LISTING_MAX_AGE_HOURS", "12"))

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


def _parse_time_on_realtor(text):
    """'5 hours ago' / 'Just listed' / '2 days ago' -> age in hours (or None)."""
    if not text:
        return None
    t = text.lower()
    if any(k in t for k in ("just listed", "just now", "today", "new listing")):
        return 0.0
    m = re.search(r"(\d+)\s*(minute|hour|day|week|month)", t)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"minute": 1 / 60, "hour": 1, "day": 24,
                "week": 168, "month": 720}[unit]


def listing_age_hours(listing, now_epoch):
    """Age of a listing in hours from InsertedDateUTC, else TimeOnRealtor.

    Returns a float, or None when neither signal is available.
    """
    ep = listing.get("listed_epoch")
    if ep:
        return max(0.0, (now_epoch - ep) / 3600.0)
    return _parse_time_on_realtor(listing.get("time_on_realtor"))


def age_label(hours):
    """Human 'listed' label from an age in hours."""
    if hours is None:
        return ""
    if hours < 1:
        return f"{int(round(hours * 60))} min ago"
    if hours < 24:
        return f"{int(round(hours))} hr ago"
    return f"{int(round(hours / 24))} day(s) ago"


def format_listing_message(listing, age_hours=None):
    bits = []
    if listing["bedrooms"]:
        bits.append(f"{listing['bedrooms']} bed")
    if listing["bathrooms"]:
        bits.append(f"{listing['bathrooms']} bath")
    if listing["size"]:
        bits.append(listing["size"])
    spec = " · ".join(bits)

    ptype = listing["type"] or "Residential"

    # Prefer Realtor.ca's own "5 hours ago" string; else our computed label.
    listed = listing.get("time_on_realtor") or age_label(age_hours)

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
    if listed:
        msg += f"🕒 Listed: {listed}\n"
    msg += (
        f"\n"
        f"🔗 {listing['url']}"
    )
    return msg[:3900]


def main():
    parser = argparse.ArgumentParser(description="Calgary Listing Alert Sender")
    parser.add_argument("--min", type=int, default=PRICE_MIN, help="Min price")
    parser.add_argument("--max", type=int, default=PRICE_MAX, help="Max price")
    args = parser.parse_args()

    age_cap = f"≤ {MAX_AGE_HOURS:g}h" if MAX_AGE_HOURS > 0 else "off"
    print(f"\n{'='*60}")
    print(f"CALGARY LISTINGS — {datetime.now().strftime('%Y-%m-%d %H:%M')} MST")
    print(f"Price window: ${args.min:,}–${args.max:,} | Freshness: {age_cap} | "
          f"Recipients: {len(CHAT_IDS)}")
    print(f"{'='*60}")

    if not BOT_TOKEN:
        print("[ERROR] TELEGRAM_BOT_TOKEN not set — cannot send.")
    if not CHAT_IDS:
        print("[ERROR] No recipients — set TELEGRAM_CHAT_ID or "
              "TELEGRAM_CHAT_ID_LISTINGS. Continuing (dry run).")

    seen = load_seen_mls()
    print(f"Tracker: {len(seen)} MLS numbers already seen")

    # Immediate confirmation that a run kicked off.
    fresh_note = (f"🕒 only listings from the last {MAX_AGE_HOURS:g}h"
                  if MAX_AGE_HOURS > 0 else "🕒 all new listings")
    start_msg = (
        f"🚀 Calgary listings scan started "
        f"({datetime.now().strftime('%Y-%m-%d %H:%M')} MST)\n"
        f"💰 ${args.min:,}–${args.max:,} · condos / townhouses / "
        f"semi-detached / houses (resale)\n"
        f"{fresh_note}\n"
        f"⏳ New listings will land here shortly."
    )
    for cid in CHAT_IDS:
        tg_send(start_msg, cid)

    listings = fetch_listings(args.min, args.max)

    now_epoch = datetime.now(timezone.utc).timestamp()
    # Is any recency signal present at all? If Realtor.ca ever stops returning
    # InsertedDateUTC/TimeOnRealtor, we degrade gracefully to dedup-only rather
    # than silently dropping everything.
    have_ts = any(listing_age_hours(l, now_epoch) is not None for l in listings)
    if MAX_AGE_HOURS > 0 and not have_ts:
        print("  [WARN] No listing timestamps in results — cannot apply the "
              f"{MAX_AGE_HOURS:g}h freshness cap; falling back to dedup-only.")

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

        # Freshness cap: only surface listings put up within MAX_AGE_HOURS.
        age = listing_age_hours(listing, now_epoch)
        if MAX_AGE_HOURS > 0 and have_ts:
            if age is None or age > MAX_AGE_HOURS:
                continue

        msg = format_listing_message(listing, age_hours=age)
        age_disp = age_label(age) or "age n/a"
        print(f"  -> {listing['mls']} | {listing['price']} | "
              f"{age_disp} | {listing['address']}")

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

    fresh_txt = f" · last {MAX_AGE_HOURS:g}h" if MAX_AGE_HOURS > 0 else ""
    summary_msg = (
        f"✅ Calgary listings scan done — "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} MST\n"
        f"{sent} new listing(s) · window ${args.min:,}–${args.max:,}{fresh_txt}\n"
        f"Source: Realtor.ca"
    )
    print(f"\n{summary_msg}")
    for cid in CHAT_IDS:
        tg_send(summary_msg, cid)

    print(f"{'='*60}\nDONE — {sent} new listings")


if __name__ == "__main__":
    main()
