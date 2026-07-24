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
import html
import json
import argparse
import urllib.parse
import urllib.request
import urllib.error
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path

from realtor_client import fetch_listings
from communities import community_info

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
    "row",              # row / townhouse
    "townhouse",
    "semi",             # semi-detached
    "duplex",
    "house",            # detached
]

# ── Condo exclusion ──────────────────────────────────────────────────────────
# "No condos" — drop apartment-style condo listings. Ground-oriented townhouses
# / row houses stay (they're house-like even if condo-titled).
EXCLUDE_CONDOS = os.environ.get("LISTING_EXCLUDE_CONDOS", "1") not in ("0", "", "false")
CONDO_MARKERS  = ["apartment", "condo apartment", "high rise", "hi-rise",
                  "highrise", "apartment unit"]

# ── Value ranking ────────────────────────────────────────────────────────────
# Keep only listings whose area-to-price ratio (sqft per $, i.e. the best value)
# falls in the top N% of the current market. Internally we rank by price/sqft
# (lower = better value); the top VALUE_TOP_PCT% by area/price == the lowest
# VALUE_TOP_PCT% by price/sqft.
VALUE_TOP_PCT = float(os.environ.get("LISTING_VALUE_TOP_PCT", "30"))
# Drop listings with no interior area (can't compute the ratio). Set to 0 to
# instead let un-rated listings through the value gate.
REQUIRE_AREA  = os.environ.get("LISTING_REQUIRE_AREA", "1") not in ("0", "", "false")
# Need at least this many rated listings before a percentile is meaningful.
MIN_VALUE_POP = 8

# ── New-build exclusion ──────────────────────────────────────────────────────
# You want resale ("old builds"), not builder new-construction. Realtor.ca is
# overwhelmingly resale, but a few new-construction listings slip in — drop the
# obvious ones by keyword in the type/address text.
NEW_BUILD_MARKERS = [
    "new construction", "to be built", "under construction",
    "pre-construction", "presale", "pre-sale", "builder",
]

# ── Distressed / motivated-seller detection (from PublicRemarks) ──────────────
# Distressed listings are ALWAYS surfaced and branded, bypassing the value and
# freshness gates, so you never miss a potential deal.
DISTRESS_STRONG = [
    "foreclosure", "judicial sale", "court order", "court-order",
    "court ordered", "power of sale", "bank owned", "bank-owned",
    "receiver", "receivership", "bankruptcy", "estate sale", "probate",
    "tax sale", "distress",
]
DISTRESS_SOFT = [
    "motivated seller", "motivated", "must sell", "priced to sell",
    "bring all offers", "bring an offer", "as is", "as-is", "as is where is",
    "quick possession", "handyman", "fixer", "fixer-upper", "tlc",
    "needs work", "renovation opportunity", "below market", "urgent sale",
    "relocating", "sold as is", "great investment", "handyman special",
]

# Feature highlights parsed from PublicRemarks (income + desirability signals).
FEATURE_MARKERS = [
    ("legal suite",        "💰 suite (rental income)"),
    ("legal basement",     "💰 suite (rental income)"),
    ("secondary suite",    "💰 suite (rental income)"),
    ("basement suite",     "💰 suite (rental income)"),
    ("illegal suite",      "💰 suite (rental income)"),
    ("walk-out",           "🏔️ walkout basement"),
    ("walkout",            "🏔️ walkout basement"),
    ("backing onto",       "🌳 backs onto green space"),
    ("backs onto",         "🌳 backs onto green space"),
    ("backs on to",        "🌳 backs onto green space"),
    ("corner lot",         "📐 corner lot"),
    ("pie lot",            "📐 large pie lot"),
    ("pie-shaped",         "📐 large pie lot"),
    ("rv parking",         "🚐 RV parking"),
    ("triple garage",      "🚗 triple garage"),
    ("heated garage",      "🚗 heated garage"),
    ("double garage",      "🚗 double garage"),
    ("oversized garage",   "🚗 oversized garage"),
    ("newly renovated",    "✨ renovated"),
    ("fully renovated",    "✨ renovated"),
    ("newer roof",         "✨ recent updates"),
    ("air condition",      "❄️ A/C"),
]


def tg_send(text, chat_id, html_mode=True):
    """Send a message to Telegram (HTML parse mode). Auto-trims to 4000 chars."""
    if len(text) > 4000:
        text = text[:3990] + "..."
    body = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_web_page_preview": True,
    }
    if html_mode:
        body["parse_mode"] = "HTML"
    payload = json.dumps(body).encode("utf-8")
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
    """Property-type allow-list + condo + new-build exclusion."""
    ptype = listing["type"].lower()
    haystack = f"{listing['type']} {listing['address']}".lower()
    if any(m in haystack for m in NEW_BUILD_MARKERS):
        return False
    if EXCLUDE_CONDOS and any(m in ptype for m in CONDO_MARKERS):
        return False
    if not WANTED_TYPES:
        return True
    return any(w in ptype for w in WANTED_TYPES)


def value_metric(listing):
    """(value, basis): price per LOT sqft preferred (land = appreciating asset),
    else price per interior sqft. Lower value = better deal."""
    ppl = listing.get("price_per_lot_sqft")
    if ppl:
        return ppl, "lot"
    pps = listing.get("price_per_sqft")
    if pps:
        return pps, "interior"
    return None, None


def build_value_dists(listings):
    """Ascending metric distributions per basis, over the eligible market."""
    lot, interior = [], []
    for l in listings:
        if not (price_ok(l) and type_ok(l)):
            continue
        v, basis = value_metric(l)
        if basis == "lot":
            lot.append(v)
        elif basis == "interior":
            interior.append(v)
    lot.sort()
    interior.sort()
    return {"lot": lot, "interior": interior}


def _cutoff(sorted_vals, pct):
    """Metric threshold for the top pct% (or None if too few to rank)."""
    if len(sorted_vals) < MIN_VALUE_POP:
        return None
    import math
    idx = min(len(sorted_vals) - 1, max(0, math.ceil(pct / 100.0 * len(sorted_vals)) - 1))
    return sorted_vals[idx]


def value_ok(listing, dists):
    """True if the listing is in the top VALUE_TOP_PCT% by its own basis."""
    v, basis = value_metric(listing)
    if basis is None:
        return not REQUIRE_AREA
    cutoff = _cutoff(dists[basis], VALUE_TOP_PCT)
    if cutoff is None:            # not enough data in this basis — don't gate
        return True
    return v <= cutoff


def value_percentile(listing, dists):
    """0.0 (best value in market) .. 1.0 (worst). None if unrated."""
    v, basis = value_metric(listing)
    if basis is None:
        return None
    arr = dists.get(basis) or []
    if not arr:
        return None
    return bisect_left(arr, v) / len(arr)


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


def distress_flags(listing):
    """(is_distressed, label). Scans PublicRemarks for distress/motivation."""
    text = (listing.get("remarks") or "").lower()
    if not text:
        return False, ""
    for kw in DISTRESS_STRONG:
        if kw in text:
            return True, kw.replace("-", " ").title()
    for kw in DISTRESS_SOFT:
        if kw in text:
            return True, "Motivated seller"
    return False, ""


def build_highlights(listing, info):
    """Merge community facts + listing features into a highlight list."""
    hi = list(info.get("highlights", []))

    # Parking (from the Parking array), if not already covered by remarks.
    pt = listing.get("parking_total") or 0
    names = " ".join(listing.get("parking_names") or []).lower()
    if "garage" in names or pt >= 2:
        if pt >= 3:
            hi.append(f"🚗 {pt} parking / garage")
        elif "garage" in names:
            hi.append("🚗 garage parking")
        elif pt >= 2:
            hi.append(f"🅿️ {pt} parking spaces")

    # Feature keywords from the description.
    text = (listing.get("remarks") or "").lower()
    for kw, label in FEATURE_MARKERS:
        if kw in text and label not in hi:
            hi.append(label)

    # De-dupe preserving order, cap length for a tidy message.
    seen, out = set(), []
    for h in hi:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out[:6]


def rating_1to10(listing, dists, info, distressed):
    """Blend value rank + location + type + motivation into a 1–10 score."""
    # Value (0–5): best value in the market scores highest.
    p = value_percentile(listing, dists)
    if p is None:
        value_pts = 2                      # un-rated -> neutral
    elif p <= 0.10:
        value_pts = 5
    elif p <= 0.20:
        value_pts = 4
    elif p <= 0.40:
        value_pts = 3
    elif p <= 0.60:
        value_pts = 2
    else:
        value_pts = 1

    # Location (0–3) from the community score (2..5 -> 0..3).
    location_pts = min(3, max(0, info.get("location_score", 2) - 2))

    # Property type (0–1): land-owning types score for appreciation.
    t = (listing.get("type") + " " + listing.get("prop_type")).lower()
    type_pts = 1 if any(k in t for k in ("house", "single family", "semi", "duplex")) else 0

    motivated_pts = 1 if distressed else 0

    return max(1, min(10, value_pts + location_pts + type_pts + motivated_pts))


def maps_link(address):
    """Google Maps search URL for an address (opens the location)."""
    a = address or ""
    if "calgary" not in a.lower():
        a = f"{a}, Calgary, AB"
    q = urllib.parse.quote(a)
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def _esc(s):
    return html.escape(str(s or ""))


def format_listing_message(listing, age_hours=None, dists=None):
    dists = dists or {"lot": [], "interior": []}
    slug = listing.get("community") or ""
    info = community_info(slug) if slug else {"name": "", "highlights": [],
                                              "location_score": 2}
    distressed, dlabel = distress_flags(listing)
    score = rating_1to10(listing, dists, info, distressed)

    # Spec line.
    bits = []
    if listing["bedrooms"]:
        bits.append(f"{listing['bedrooms']} bed")
    if listing["bathrooms"]:
        bits.append(f"{listing['bathrooms']} bath")
    if listing.get("size_sqft"):
        bits.append(f"{listing['size_sqft']:,.0f} sqft")
    if listing.get("lot_sqft"):
        bits.append(f"lot {listing['lot_sqft']:,.0f} sqft")
    spec = " · ".join(bits)

    ptype = listing.get("type") or "Residential"
    listed = listing.get("time_on_realtor") or age_label(age_hours)
    dom = f"{int(round(age_hours / 24))}d on market" if age_hours and age_hours >= 24 else ""

    stars = "⭐" * int(round(score / 2)) or "⭐"
    address = listing["address"] or "Address not listed"
    community_line = _esc(info["name"]) if info["name"] else ""

    # Value metric line (lot-based preferred).
    val_line = ""
    v, basis = value_metric(listing)
    if basis == "lot":
        val_line = f"🌳 <b>${v:,.0f}/lot-sqft</b> (land value)"
    elif basis == "interior":
        val_line = f"📐 <b>${v:,.0f}/sqft</b> (area value)"
    else:
        val_line = "📐 area/lot not listed — review manually"

    lines = []
    if distressed:
        lines.append(f"🔥🔥 <b>DISTRESSED / {_esc(dlabel).upper()}</b> 🔥🔥")
    lines.append(f"🏠 <b>{_esc(ptype)}</b> — MLS# {_esc(listing['mls'])}")
    lines.append(f"💰 <b>{_esc(listing['price'] or 'Price n/a')}</b>  ·  {stars} <b>{score}/10</b>")
    lines.append(f"📍 <a href=\"{maps_link(address)}\">{_esc(address)}</a>  🗺️")
    if community_line:
        lines.append(f"🏘️ <b>{community_line}</b>")
    if spec:
        lines.append(f"🛏️ {_esc(spec)}")
    lines.append(val_line)
    if listing.get("condo_fee"):
        lines.append(f"💵 Fee: {_esc(listing['condo_fee'])}")
    fresh = " · ".join(x for x in [f"🕒 {listed}" if listed else "", dom] if x)
    if fresh:
        lines.append(fresh)
    hl = build_highlights(listing, info)
    if hl:
        lines.append("✨ " + "  ·  ".join(hl))
    lines.append(f"🔗 <a href=\"{_esc(listing['url'])}\">View on Realtor.ca</a>")

    return "\n".join(lines)[:3900]


def main():
    parser = argparse.ArgumentParser(description="Calgary Listing Alert Sender")
    parser.add_argument("--min", type=int, default=PRICE_MIN, help="Min price")
    parser.add_argument("--max", type=int, default=PRICE_MAX, help="Max price")
    args = parser.parse_args()

    age_cap = f"≤ {MAX_AGE_HOURS:g}h" if MAX_AGE_HOURS > 0 else "off"
    print(f"\n{'='*60}")
    print(f"CALGARY LISTINGS — {datetime.now().strftime('%Y-%m-%d %H:%M')} MST")
    print(f"Price ${args.min:,}–${args.max:,} | Freshness: {age_cap} | "
          f"Value: top {VALUE_TOP_PCT:g}% lot/price (area fallback) | "
          f"Condos: {'excluded' if EXCLUDE_CONDOS else 'included'} | "
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
        f"💰 ${args.min:,}–${args.max:,} · townhouses / semi / houses (no condos)\n"
        f"🌳 only top {VALUE_TOP_PCT:g}% by lot-to-price value (+ ratings & highlights)\n"
        f"🔥 all distressed / motivated sales flagged\n"
        f"{fresh_note}\n"
        f"⏳ New value picks will land here shortly."
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

    # Value ranking: build price-per-unit distributions from the WHOLE market
    # (all non-condo, in-price listings), per basis (lot preferred, else
    # interior), so "top X%" is measured against the market. Log lot coverage.
    dists = build_value_dists(listings)
    lot_n, int_n = len(dists["lot"]), len(dists["interior"])
    lot_cut = _cutoff(dists["lot"], VALUE_TOP_PCT)
    int_cut = _cutoff(dists["interior"], VALUE_TOP_PCT)
    print(f"  [Value] market: {lot_n} with lot size, {int_n} with interior only.")
    if lot_cut:
        print(f"          top {VALUE_TOP_PCT:g}% lot cutoff = ${lot_cut:,.0f}/lot-sqft")
    if int_cut:
        print(f"          top {VALUE_TOP_PCT:g}% area cutoff = ${int_cut:,.0f}/sqft")

    sent = distressed_sent = 0
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

        # Distressed / motivated sales are ALWAYS surfaced — they bypass the
        # freshness and value gates so a potential deal is never missed.
        is_distressed, _dlabel = distress_flags(listing)

        age = listing_age_hours(listing, now_epoch)
        if not is_distressed:
            # Freshness cap: only listings put up within MAX_AGE_HOURS.
            if MAX_AGE_HOURS > 0 and have_ts:
                if age is None or age > MAX_AGE_HOURS:
                    continue
            # Value gate: only the top VALUE_TOP_PCT% by price-per-unit.
            if not value_ok(listing, dists):
                continue

        msg = format_listing_message(listing, age_hours=age, dists=dists)
        age_disp = age_label(age) or "age n/a"
        v, basis = value_metric(listing)
        v_disp = f"${v:,.0f}/{basis[:3]}" if basis else "n/a"
        tag = " 🔥DISTRESSED" if is_distressed else ""
        print(f"  -> {listing['mls']} | {listing['price']} | {v_disp} | "
              f"{age_disp}{tag} | {listing['address']}")

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
            if is_distressed:
                distressed_sent += 1

    fresh_txt = f" · last {MAX_AGE_HOURS:g}h" if MAX_AGE_HOURS > 0 else ""
    dist_txt = f" · 🔥 {distressed_sent} distressed" if distressed_sent else ""
    summary_msg = (
        f"✅ Calgary listings scan done — "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} MST\n"
        f"{sent} new pick(s){dist_txt} · ${args.min:,}–${args.max:,}{fresh_txt} · "
        f"top {VALUE_TOP_PCT:g}% value\n"
        f"Source: Realtor.ca"
    )
    print(f"\n{summary_msg}")
    for cid in CHAT_IDS:
        tg_send(summary_msg, cid)

    print(f"{'='*60}\nDONE — {sent} new listings")


if __name__ == "__main__":
    main()
