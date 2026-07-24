#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
communities.py — Calgary community knowledge base.

Maps a listing's community (parsed from its Realtor.ca URL slug) to factual
neighbourhood attributes: quadrant, proximity to CTrain / Stoney Trail /
Deerfoot, lakes, major parks, school desirability, rental demand, and
inner-city land value. Used to build message highlights and a location score.

These are factual, well-known attributes of established Calgary communities.
Coverage is broad but not exhaustive — an unknown community still works (it
just gets fewer highlights and a neutral location score).
"""

# ── Attribute sets (community slugs, hyphen-lowercase) ────────────────────────

LAKE = {  # communities with private/lake access
    "auburn-bay", "mahogany", "arbour-lake", "chaparral", "lake-bonavista",
    "midnapore", "sundance", "mckenzie-lake",
}

LRT = {  # communities at/near a CTrain station
    "tuscany", "arbour-lake", "citadel", "ranchlands", "hawkwood", "dalhousie",
    "brentwood", "varsity", "university-heights", "st-andrews-heights",
    "banff-trail", "capitol-hill", "erlton", "manchester", "windsor-park",
    "meadowlark-park", "kelvin-grove", "haysboro", "kingsland", "southwood",
    "willow-park", "fairview", "canyon-meadows", "midnapore", "shawnessy",
    "millrise", "somerset", "bridlewood", "sunalta", "shaganappi", "westgate",
    "rosscarrock", "spruce-cliff", "glendale", "marlborough", "marlborough-park",
    "rundle", "whitehorn", "martindale", "taradale", "saddle-ridge", "franklin",
}

STONEY = {  # quick access to Stoney Trail ring road
    "saddle-ridge", "redstone", "cityscape", "skyview-ranch", "cornerstone",
    "martindale", "taradale", "sherwood", "kincora", "nolan-hill", "sage-hill",
    "evanston", "symons-valley", "carrington", "livingston", "ambleton",
    "glacier-ridge", "auburn-bay", "mahogany", "cranston", "seton", "copperfield",
    "new-brighton", "mckenzie-towne", "silverado", "legacy", "walden",
    "chaparral", "yorkville", "belmont", "pine-creek", "providence", "rangeview",
}

DEERFOOT = {  # near the Deerfoot Trail (QEII) corridor
    "douglasdale", "douglas-glen", "mckenzie-lake", "mckenzie-towne",
    "deer-ridge", "deer-run", "copperfield", "new-brighton", "cranston", "seton",
    "sundance", "parkland", "queensland", "riverbend", "ogden", "lynnwood",
    "saddle-ridge", "whitehorn", "mayland-heights", "vista-heights",
}

FISH_CREEK = {  # backing onto / beside Fish Creek Provincial Park
    "bridlewood", "evergreen", "millrise", "shawnessy", "midnapore", "sundance",
    "deer-run", "deer-ridge", "parkland", "bonavista-downs", "canyon-meadows",
    "queensland", "douglasdale",
}

NOSE_HILL = {  # beside Nose Hill Park
    "edgemont", "brentwood", "charleswood", "collingwood", "cambrian-heights",
    "huntington-hills", "thorncliffe", "macewan-glen", "north-haven",
}

BOW_RIVER = {  # on the Bow River pathway network
    "bowness", "montgomery", "point-mckay", "parkdale", "sunnyside", "hillhurst",
    "west-hillhurst", "inglewood", "riverbend", "quarry-park", "douglasdale",
    "cranston", "eau-claire", "sandstone-valley",
}

UNIVERSITY_RENTAL = {  # strong rental demand (near U of C / SAIT / downtown / hospitals)
    "university-heights", "varsity", "brentwood", "charleswood",
    "university-district", "banff-trail", "capitol-hill", "st-andrews-heights",
    "montgomery", "hounsfield-heights-briar-hill", "tuxedo-park", "renfrew",
    "bridgeland", "sunnyside", "hillhurst", "west-hillhurst", "beltline",
    "downtown", "downtown-west-end", "mission", "cliff-bungalow",
    "lower-mount-royal", "bankview", "sunalta",
}

INNER_CITY = {  # established inner-city — land value / appreciation
    "altadore", "garrison-woods", "garrison-green", "south-calgary", "killarney",
    "glengarry", "glendale", "glenbrook", "richmond", "knob-hill", "bankview",
    "scarboro", "sunalta", "shaganappi", "spruce-cliff", "wildwood",
    "rosscarrock", "westgate", "bridgeland", "renfrew", "rosedale",
    "mount-pleasant", "capitol-hill", "tuxedo-park", "crescent-heights",
    "sunnyside", "hillhurst", "west-hillhurst", "hounsfield-heights-briar-hill",
    "parkdale", "point-mckay", "elbow-park", "britannia", "mayfair",
    "meadowlark-park", "windsor-park", "mission", "cliff-bungalow",
    "lower-mount-royal", "ramsay", "inglewood", "winston-heights",
    "highland-park", "banff-trail", "st-andrews-heights", "rideau-park",
    "roxboro", "elboya", "kelvin-grove",
}

GOOD_SCHOOLS = {  # sought-after school catchments
    "tuscany", "cranston", "auburn-bay", "mahogany", "mckenzie-towne",
    "bridlewood", "evergreen", "signal-hill", "aspen-woods", "springbank-hill",
    "west-springs", "cougar-ridge", "edgemont", "hamptons", "arbour-lake",
    "panorama-hills", "coventry-hills", "nolan-hill", "sage-hill",
    "scenic-acres", "citadel", "hidden-valley", "sherwood", "kincora",
    "altadore", "elbow-park", "brentwood", "varsity", "university-district",
    "rocky-ridge", "royal-oak",
}

# Quadrant hints for common suffixes / known communities (display only).
_QUADRANTS = {
    "ne": "NE", "nw": "NW", "se": "SE", "sw": "SW",
}


def community_from_url(url):
    """Extract the community slug from a Realtor.ca listing URL.

    e.g. '.../516-auburn-bay-circle-se-calgary-auburn-bay' -> 'auburn-bay'
    """
    if not url:
        return ""
    seg = url.rstrip("/").split("/")[-1].lower()
    if "-calgary-" in seg:
        return seg.split("-calgary-")[-1].strip()
    return ""


def community_name(slug):
    """'auburn-bay' -> 'Auburn Bay'."""
    return " ".join(w.capitalize() for w in slug.split("-")) if slug else ""


def community_info(slug):
    """Return {name, highlights: [str], location_score: 1-5} for a slug."""
    hi = []
    if slug in INNER_CITY:
        hi.append("📈 established inner-city")
    if slug in LAKE:
        hi.append("🏊 lake community")
    if slug in LRT:
        hi.append("🚉 CTrain nearby")
    if slug in STONEY:
        hi.append("🛣️ Stoney Trail access")
    if slug in DEERFOOT:
        hi.append("🚗 Deerfoot access")
    if slug in FISH_CREEK:
        hi.append("🌳 by Fish Creek Park")
    if slug in NOSE_HILL:
        hi.append("🌳 by Nose Hill Park")
    if slug in BOW_RIVER:
        hi.append("🏞️ Bow River pathways")
    if slug in GOOD_SCHOOLS:
        hi.append("🏫 sought-after schools")
    if slug in UNIVERSITY_RENTAL:
        hi.append("🎓 strong rental demand")

    # Location score 1-5 from the strongest signals.
    score = 2
    if slug in INNER_CITY or slug in LAKE:
        score += 1
    if slug in GOOD_SCHOOLS:
        score += 1
    if slug in LRT or slug in UNIVERSITY_RENTAL:
        score += 1
    score = max(1, min(5, score))

    return {
        "name": community_name(slug),
        "highlights": hi,
        "location_score": score,
    }
