#!/usr/bin/env python3
"""
IRAN WATCH — Daily Update Script
Runs server-side (GitHub Actions) at 0500 GMT daily.
Fetches live data from free APIs, sends to Claude for analysis,
and generates an updated static HTML file.

SETUP (one time):
1. Create a GitHub repo and add this file + template.html
2. Get a free Anthropic API key from https://console.anthropic.com/
3. Get a free OpenSky account from https://opensky-network.org/
   → Go to Account page → Create an API client → note client_id and client_secret
4. In your GitHub repo: Settings > Secrets > Actions, add:
     ANTHROPIC_API_KEY    = your Claude API key
     OPENSKY_CLIENT_ID    = your OpenSky API client ID
     OPENSKY_CLIENT_SECRET = your OpenSky API client secret
5. Add the GitHub Actions workflow file (see .github/workflows/update.yml)
6. Enable GitHub Pages on the repo (Settings > Pages > main branch)

COST: Claude Haiku 4.5 costs roughly $0.01-0.03 per daily update.
      ~$1/month for daily updates. All other APIs are free.
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from string import Template

import requests

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "")

# Middle East bounding box for aircraft queries
ME_BBOX = {"lamin": 12, "lamax": 42, "lomin": 25, "lomax": 70}

# Military callsign prefixes (US/NATO airlift, tanker, bomber, ISR, fighter)
MIL_PREFIXES = [
    # US Airlift (C-17, C-5, C-130)
    "RCH", "REACH", "PACK", "DUKE", "MOOSE", "FRED",
    "CARGO", "HERK", "HERKY",
    # US Tanker (KC-135, KC-46, KC-10)
    "ETHYL", "JULIET", "PEARL", "STEEL", "SHELL",
    "TEAL", "BRIT", "NKAC", "PKSN",
    # US Bomber (B-2, B-52, B-1)
    "DOOM", "DEATH", "BATT", "SEVILLE", "MYTEE",
    "BONE", "LANCE", "TIGER",
    # US ISR / SIGINT / AWACS
    "HOMER", "TOPCT", "JAKE", "TITAN", "FORTE",
    "MAGIC", "SNTRY", "REDEYE", "RAIDR", "OLIVE",
    "MAZDA", "TANGO", "NCHO",
    # US Fighter / Strike
    "VIPER", "EAGLE", "HAWK", "RAZOR", "STRIKE",
    "TREND", "RAGE", "BOLT", "WRATH",
    # US CSAR / Medevac / Special
    "KING", "EVAC", "PEDRO", "JOLLY", "DUSTOFF",
    # US VIP / Command
    "SAM", "VENUS", "EXEC", "SPAR", "IRON",
    # US Navy / Marine
    "NAVY", "HAVOC", "CONDOR",
    # UK RAF
    "ASCOT", "TARTN", "RRR",
    # Other NATO / Coalition
    "GAF", "FAF", "IAM", "BAF", "DAF",
    # Generic military patterns
    "GOLD", "SHADOW", "TORCH",
]

# Also detect military by origin country (US, UK) + non-standard callsigns
MIL_COUNTRIES = ["United States", "United Kingdom"]

# Callsign prefix → probable airframe and role
# Sources: OSINT community databases, ADS-B Exchange, milaircomms.com
CALLSIGN_AIRFRAMES = {
    # Airlift
    "RCH":    ("C-17A Globemaster III", "Strategic airlift"),
    "REACH":  ("C-17A Globemaster III", "Strategic airlift"),
    "PACK":   ("C-17A Globemaster III", "Strategic airlift"),
    "DUKE":   ("C-17A Globemaster III", "Strategic airlift"),
    "MOOSE":  ("C-5M Super Galaxy", "Heavy airlift"),
    "FRED":   ("C-5M Super Galaxy", "Heavy airlift"),
    "CARGO":  ("C-17A / C-5M", "Airlift"),
    "HERK":   ("C-130J Super Hercules", "Tactical airlift"),
    "HERKY":  ("C-130J Super Hercules", "Tactical airlift"),
    # Tankers
    "ETHYL":  ("KC-135 Stratotanker", "Aerial refueling"),
    "JULIET": ("KC-10 Extender", "Aerial refueling"),
    "PEARL":  ("KC-135 Stratotanker", "Aerial refueling"),
    "STEEL":  ("KC-46A Pegasus", "Aerial refueling"),
    "SHELL":  ("KC-135 Stratotanker", "Aerial refueling"),
    "TEAL":   ("KC-135 Stratotanker", "Aerial refueling"),
    "NKAC":   ("KC-135 Stratotanker", "Aerial refueling"),
    "PKSN":   ("KC-46A Pegasus", "Aerial refueling"),
    # Bombers
    "DOOM":   ("B-2A Spirit", "Stealth bomber — HIGH SIGNIFICANCE"),
    "DEATH":  ("B-52H Stratofortress", "Strategic bomber"),
    "BATT":   ("B-52H Stratofortress", "Strategic bomber"),
    "MYTEE":  ("B-52H Stratofortress", "Strategic bomber"),
    "BONE":   ("B-1B Lancer", "Supersonic bomber"),
    "LANCE":  ("B-1B Lancer", "Supersonic bomber"),
    "TIGER":  ("B-1B Lancer", "Supersonic bomber"),
    # ISR / SIGINT / AWACS
    "HOMER":  ("P-8A Poseidon", "Maritime patrol / ASW"),
    "TOPCT":  ("RC-135V/W Rivet Joint", "SIGINT collection"),
    "JAKE":   ("E-3 Sentry (AWACS)", "Airborne early warning"),
    "TITAN":  ("RQ-4B Global Hawk", "High-altitude ISR drone"),
    "FORTE":  ("RQ-4B Global Hawk", "High-altitude ISR drone"),
    "MAGIC":  ("E-6B Mercury", "Airborne command post — NUCLEAR C2"),
    "SNTRY":  ("E-3 Sentry (AWACS)", "Airborne early warning"),
    "REDEYE": ("RC-135U Combat Sent", "Electronic intelligence"),
    "RAIDR":  ("MC-130J Commando II", "Special operations"),
    "OLIVE":  ("RC-135S Cobra Ball", "Missile tracking"),
    "MAZDA":  ("E-8C JSTARS", "Ground surveillance"),
    # Fighters / Strike
    "VIPER":  ("F-16 Fighting Falcon", "Multirole fighter"),
    "EAGLE":  ("F-15E Strike Eagle", "Air superiority / strike"),
    "HAWK":   ("F-15E Strike Eagle", "Air superiority"),
    "RAZOR":  ("F-22A Raptor", "Air superiority — stealth"),
    "STRIKE": ("F-15E Strike Eagle", "Strike fighter"),
    "BOLT":   ("F-35A Lightning II", "Stealth multirole"),
    "WRATH":  ("F-15E Strike Eagle", "Strike fighter"),
    # CSAR / Medevac / Special Ops
    "KING":   ("HC-130J Combat King II", "Combat search & rescue"),
    "PEDRO":  ("HH-60W Jolly Green II", "Combat rescue helicopter"),
    "JOLLY":  ("HH-60G Pave Hawk", "Combat rescue helicopter"),
    "DUSTOFF":("UH-60 Black Hawk", "Medevac"),
    "EVAC":   ("C-17A / C-130J", "Aeromedical evacuation"),
    # VIP / Command
    "SAM":    ("VC-25A / C-32A", "VIP transport — SENIOR LEADER"),
    "VENUS":  ("C-37A Gulfstream V", "VIP transport"),
    "EXEC":   ("C-37A / C-40B", "Executive transport"),
    "SPAR":   ("C-40B Clipper", "Congressional / senior leader"),
    # Navy / Marine
    "NAVY":   ("P-8A / E-2D / C-2A", "Naval aviation"),
    "HAVOC":  ("AH-1Z / MV-22", "Marine attack aviation"),
    "CONDOR": ("C-40A Clipper", "Naval logistics"),
    # UK RAF
    "ASCOT":  ("C-17 / A400M / Voyager", "RAF transport / tanker"),
    "RRR":    ("Voyager KC3 / A330 MRTT", "RAF aerial refueling"),
    # NATO
    "GAF":    ("A400M / A310", "German Air Force transport"),
    "FAF":    ("A400M / MRTT", "French Air Force"),
    "IAM":    ("C-130J / KC-767", "Italian Air Force"),
}

def identify_airframe(callsign):
    """Return (airframe, role) tuple for a callsign, or None."""
    cs = callsign.upper()
    for prefix, info in CALLSIGN_AIRFRAMES.items():
        if cs.startswith(prefix):
            return info
    return None

# Polymarket event slugs for Iran-related markets
POLYMARKET_IRAN_SLUGS = [
    "us-strikes-iran-by",
    "usisrael-strikes-iran-by",
    "us-next-strikes-iran-on-843",
]

# ──────────────────────────────────────────────────────────────
# DATA FETCHERS
# ──────────────────────────────────────────────────────────────

def get_opensky_token():
    """Get a Bearer token from OpenSky using OAuth2 client credentials flow."""
    if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET:
        return None

    token_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    try:
        resp = requests.post(
            token_url,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "client_credentials",
                "client_id": OPENSKY_CLIENT_ID,
                "client_secret": OPENSKY_CLIENT_SECRET,
            },
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json().get("access_token")
        if token:
            print(f"[OpenSky] OAuth2 token obtained (expires in {resp.json().get('expires_in', '?')}s)")
        return token
    except Exception as e:
        print(f"[OpenSky] OAuth2 token error: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# LOCATION RESOLVER — converts lat/lon to plain English
# ──────────────────────────────────────────────────────────────

# Reference points: bases, cities, and landmarks
_REFERENCE_POINTS = [
    # US/Coalition bases
    (25.117, 51.315, "Al Udeid AB, Qatar"),
    (24.248, 54.547, "Al Dhafra AB, UAE"),
    (29.346, 47.521, "Ali Al Salem AB, Kuwait"),
    (32.356, 36.259, "Muwaffaq Salti AB, Jordan"),
    (24.062, 47.580, "Prince Sultan AB, Saudi Arabia"),
    (11.547, 43.155, "Camp Lemonnier, Djibouti"),
    (37.002, 35.426, "Incirlik AB, Turkey"),
    (34.590, 32.988, "RAF Akrotiri, Cyprus"),
    (26.236, 50.577, "NSA Bahrain"),
    (-7.313, 72.411, "Diego Garcia"),
    # Key cities
    (35.689, 51.389, "Tehran"),
    (32.621, 51.678, "Isfahan"),
    (32.064, 52.068, "Natanz"),
    (34.861, 50.988, "Fordow"),
    (27.188, 56.275, "Bandar Abbas"),
    (33.313, 44.366, "Baghdad"),
    (29.376, 47.978, "Kuwait City"),
    (25.286, 51.533, "Doha"),
    (24.454, 54.654, "Abu Dhabi"),
    (25.204, 55.271, "Dubai"),
    (23.486, 58.382, "Muscat"),
    (21.485, 39.193, "Jeddah"),
    (24.713, 46.675, "Riyadh"),
    (38.963, 35.243, "Ankara"),
    (31.768, 35.214, "Jerusalem"),
    (32.084, 34.782, "Tel Aviv"),
    (33.513, 36.292, "Damascus"),
    (36.191, 44.009, "Kirkuk"),
    (36.335, 43.119, "Mosul"),
    (30.508, 47.783, "Basra"),
    (15.370, 44.206, "Sana'a"),
    (12.778, 45.019, "Aden"),
]

# Approximate country bounding boxes: (lat_min, lat_max, lon_min, lon_max, name)
_COUNTRY_BOXES = [
    (25, 40, 44, 63, "Iran"),
    (29, 37.5, 39, 48.5, "Iraq"),
    (16, 32, 35, 56, "Saudi Arabia"),
    (22.5, 26.5, 51, 56.5, "UAE"),
    (16, 26.5, 52, 60, "Oman"),
    (28.5, 30.5, 46.5, 48.5, "Kuwait"),
    (24.5, 26.5, 50.5, 52, "Qatar"),
    (36, 42, 26, 45, "Turkey"),
    (32, 37.5, 35.5, 42, "Syria"),
    (29, 33.5, 35, 39, "Jordan"),
    (29, 33.5, 34, 35.9, "Israel"),
    (22, 31.5, 25, 37, "Egypt"),
    (12, 19, 42, 54, "Yemen"),
    (24, 37, 60, 75, "Pakistan"),
    (29, 38.5, 60, 75, "Afghanistan"),
    (34, 35.5, 32.5, 34.5, "Cyprus"),
    (10, 12, 42, 44, "Djibouti"),
    (-1, 12, 41, 51, "Somalia"),
    (12, 18, 36, 43, "Eritrea"),
]

# Water bodies
_WATER_BODIES = [
    (26, 27.5, 49, 56, "the Persian Gulf"),
    (24, 26.5, 56, 59, "the Gulf of Oman"),
    (12, 24, 36, 50, "the Red Sea"),
    (10, 20, 50, 60, "the Arabian Sea"),
    (24, 30, 33, 35, "the eastern Mediterranean"),
    (34, 37, 28, 36, "the eastern Mediterranean"),
    (12, 30, 60, 75, "the Arabian Sea"),
    (11, 13, 43, 48, "the Gulf of Aden"),
    (25.5, 27, 56, 57, "the Strait of Hormuz"),
]

def _haversine_nm(lat1, lon1, lat2, lon2):
    """Distance between two points in nautical miles."""
    R = 3440.065  # Earth radius in nm
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def describe_location(lat, lon):
    """Convert lat/lon to a human-readable location description."""
    if lat is None or lon is None:
        return "unknown location"

    # 1. Check if near a known reference point (within 40nm / ~45 miles)
    nearest_ref = None
    nearest_dist = float('inf')
    for rlat, rlon, rname in _REFERENCE_POINTS:
        d = _haversine_nm(lat, lon, rlat, rlon)
        if d < nearest_dist:
            nearest_dist = d
            nearest_ref = rname

    if nearest_dist < 15:
        return f"near {nearest_ref}"
    elif nearest_dist < 40:
        miles = round(nearest_dist * 1.151)
        return f"~{miles} mi from {nearest_ref}"

    # 2. Check if over water
    for wlat_min, wlat_max, wlon_min, wlon_max, wname in _WATER_BODIES:
        if wlat_min <= lat <= wlat_max and wlon_min <= lon <= wlon_max:
            # Find nearest coast country for reference
            nearest_country = None
            nearest_cdist = float('inf')
            for clat_min, clat_max, clon_min, clon_max, cname in _COUNTRY_BOXES:
                # Distance to nearest edge of country box
                clat = max(clat_min, min(lat, clat_max))
                clon = max(clon_min, min(lon, clon_max))
                cd = _haversine_nm(lat, lon, clat, clon)
                if cd < nearest_cdist:
                    nearest_cdist = cd
                    nearest_country = cname
            if nearest_country and nearest_cdist > 5:
                miles = round(nearest_cdist * 1.151)
                return f"over {wname}, ~{miles} mi off {nearest_country}"
            return f"over {wname}"

    # 3. Check which country it's over
    for clat_min, clat_max, clon_min, clon_max, cname in _COUNTRY_BOXES:
        if clat_min <= lat <= clat_max and clon_min <= lon <= clon_max:
            # Add regional detail if near a known city
            if nearest_dist < 100:
                miles = round(nearest_dist * 1.151)
                return f"over {cname}, ~{miles} mi from {nearest_ref}"
            return f"over {cname}"

    # 4. Fallback
    if nearest_ref:
        miles = round(nearest_dist * 1.151)
        return f"~{miles} mi from {nearest_ref}"
    return f"{lat}°N, {lon}°E"


def fetch_opensky():
    """Fetch military aircraft in the ME bounding box from OpenSky Network."""
    print("[OpenSky] Fetching aircraft data...")
    url = "https://opensky-network.org/api/states/all"
    params = {
        "lamin": ME_BBOX["lamin"],
        "lamax": ME_BBOX["lamax"],
        "lomin": ME_BBOX["lomin"],
        "lomax": ME_BBOX["lomax"],
    }

    # Authenticate via OAuth2 Bearer token (required for post-March-2025 accounts)
    headers = {}
    token = get_opensky_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
        print("[OpenSky] Using authenticated request (higher rate limits)")
    else:
        print("[OpenSky] No credentials — using anonymous request (lower rate limits)")

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        all_aircraft = data.get("states", []) or []
        mil_aircraft = []

        for ac in all_aircraft:
            callsign = (ac[1] or "").strip().upper()
            origin = ac[2] or ""
            on_ground = ac[8]
            lat, lon = ac[6], ac[5]
            alt_m = ac[7]

            # Match by callsign prefix
            is_mil = any(callsign.startswith(p) for p in MIL_PREFIXES)

            # Also flag US/UK aircraft with non-commercial callsign patterns
            # Commercial flights are typically 2-3 letter airline code + numbers (e.g., UAL123)
            if not is_mil and origin in MIL_COUNTRIES and callsign:
                # Military callsigns often have letters mixed in or are all-alpha
                has_no_digits = not any(c.isdigit() for c in callsign[:3])
                if has_no_digits and len(callsign) >= 4:
                    is_mil = True

            if is_mil and not on_ground:
                r_lat = round(lat, 2) if lat else None
                r_lon = round(lon, 2) if lon else None
                airframe = identify_airframe(callsign)
                mil_aircraft.append({
                    "callsign": callsign,
                    "origin": origin,
                    "lat": r_lat,
                    "lon": r_lon,
                    "alt_ft": round(alt_m * 3.281) if alt_m else None,
                    "location_desc": describe_location(r_lat, r_lon) if r_lat and r_lon else "unknown",
                    "airframe": airframe[0] if airframe else "Unknown type",
                    "role": airframe[1] if airframe else "Military",
                })

        print(f"[OpenSky] Total aircraft in ME box: {len(all_aircraft)}, "
              f"Possible military: {len(mil_aircraft)}")
        return {
            "status": "ok",
            "total_aircraft": len(all_aircraft),
            "mil_count": len(mil_aircraft),
            "mil_aircraft": mil_aircraft[:30],  # top 30
        }

    except Exception as e:
        print(f"[OpenSky] Error: {e}")
        return {"status": "error", "error": str(e), "mil_count": 0, "mil_aircraft": []}


def fetch_polymarket():
    """Fetch Iran-related prediction markets from Polymarket Gamma API."""
    print("[Polymarket] Fetching Iran markets...")
    url = "https://gamma-api.polymarket.com/events"

    try:
        # Search by tag — cast a wide net
        markets = []
        for tag in ["iran", "middle-east", "geopolitics"]:
            try:
                resp = requests.get(url, params={
                    "tag": tag, "active": "true", "closed": "false", "limit": 50
                }, timeout=15)
                resp.raise_for_status()
                events = resp.json()

                for ev in events:
                    for m in ev.get("markets", []):
                        q = (m.get("question") or "").lower()
                        # Exclude sports, entertainment, non-conflict markets
                        if any(ex in q for ex in [
                            "world cup", "soccer", "football", "olympics", "fifa",
                            "medal", "qualify", "championship", "tournament",
                            "movie", "album", "grammy", "oscar", "box office",
                            "gdp", "inflation", "interest rate", "bitcoin",
                        ]):
                            continue
                        # Include markets about Iran conflict / military / nuclear
                        if any(kw in q for kw in [
                            "iran", "tehran", "khamenei", "irgc", "fordow", "natanz",
                            "strike", "centcom", "persian gulf", "strait of hormuz",
                            "arabian sea", "nuclear", "enrichment", "regime change"
                        ]):
                            prices = json.loads(m.get("outcomePrices", "[]"))
                            yes_price = round(float(prices[0]) * 100) if prices else None
                            if yes_price is not None:
                                mid = m.get("id", "")
                                # Avoid duplicates
                                if not any(x["question"] == m.get("question") for x in markets):
                                    markets.append({
                                        "question": m.get("question", ""),
                                        "probability": yes_price,
                                        "volume": m.get("volume", "0"),
                                        "url": f"https://polymarket.com/event/{ev.get('slug', '')}",
                                    })
            except Exception:
                pass  # Continue with next tag

        # Sort by volume (most liquid markets first)
        markets.sort(key=lambda x: float(x.get("volume", 0)), reverse=True)

        print(f"[Polymarket] Found {len(markets)} Iran-related markets")
        return {"status": "ok", "markets": markets[:10]}

    except Exception as e:
        print(f"[Polymarket] Error: {e}")
        return {"status": "error", "error": str(e), "markets": []}


def fetch_metaculus():
    """Fetch Iran-related forecasting questions from Metaculus API."""
    print("[Metaculus] Fetching Iran questions...")

    try:
        # Try the v2 API first, fall back to legacy
        resp = requests.get(
            "https://www.metaculus.com/api2/questions/",
            params={"search": "iran", "status": "open", "limit": 20, "type": "binary",
                    "order_by": "-activity"},
            timeout=15,
            headers={"Accept": "application/json"},
        )
        if resp.status_code == 404:
            # Try alternative endpoint
            resp = requests.get(
                "https://www.metaculus.com/api/questions/",
                params={"search": "iran", "status": "open", "limit": 20},
                timeout=15,
                headers={"Accept": "application/json"},
            )
        resp.raise_for_status()
        data = resp.json()

        questions = []
        for q in data.get("results", []):
            title = (q.get("title") or "").lower()
            if "iran" in title:
                cp = q.get("community_prediction", {})
                full = cp.get("full", {}) if isinstance(cp, dict) else {}
                median = full.get("q2") if isinstance(full, dict) else None
                if median is not None:
                    questions.append({
                        "question": q.get("title", ""),
                        "probability": round(median * 100),
                        "forecasters": q.get("number_of_predictions", 0),
                        "url": q.get("url", ""),
                    })

        print(f"[Metaculus] Found {len(questions)} Iran-related questions")
        return {"status": "ok", "questions": questions[:10]}

    except Exception as e:
        print(f"[Metaculus] Error: {e}")
        return {"status": "error", "error": str(e), "questions": []}


def fetch_centcom_rss():
    """Fetch latest CENTCOM press releases via RSS."""
    print("[CENTCOM] Fetching RSS feed...")

    try:
        headers = {"User-Agent": "IranWatch/1.0 (OSINT Monitor; +https://github.com)"}
        resp = requests.get("https://www.centcom.mil/RSS/", headers=headers, timeout=15)
        if resp.status_code == 403:
            # Try alternative CENTCOM feed
            resp = requests.get(
                "https://www.centcom.mil/MEDIA/PRESS-RELEASES/",
                headers=headers, timeout=15
            )
        resp.raise_for_status()
        # Simple XML parsing for titles — no external dependency needed
        import re
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", resp.text)
        if not titles:
            titles = re.findall(r"<title>(.*?)</title>", resp.text)

        releases = [t.strip() for t in titles[:15] if t.strip() and "CENTCOM" not in t[:10]]
        print(f"[CENTCOM] Found {len(releases)} recent releases")
        return {"status": "ok", "releases": releases}

    except Exception as e:
        print(f"[CENTCOM] Error: {e}")
        return {"status": "error", "error": str(e), "releases": []}


# ──────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS
# ──────────────────────────────────────────────────────────────

def generate_analysis(opensky, polymarket, metaculus, centcom):
    """Send all collected data to Claude API for IC-style analysis."""
    print("[Claude] Generating analysis...")

    if not ANTHROPIC_API_KEY:
        print("[Claude] WARNING: No ANTHROPIC_API_KEY set. Using fallback analysis.")
        return generate_fallback_analysis(opensky, polymarket, metaculus, centcom)

    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%A, %d %B %Y")

    data_summary = f"""
## LIVE DATA COLLECTED AT {now_utc.strftime('%Y-%m-%d %H:%M UTC')}

### OpenSky Network (Aircraft in ME bounding box lat 12-42, lon 25-70)
Status: {opensky['status']}
Total aircraft detected: {opensky.get('total_aircraft', 'N/A')}
Possible military aircraft (matching mil callsign prefixes): {opensky.get('mil_count', 0)}
Military aircraft details: {json.dumps(opensky.get('mil_aircraft', [])[:15], indent=2)}

### Polymarket Iran Prediction Markets
Status: {polymarket['status']}
Markets found: {len(polymarket.get('markets', []))}
Market data: {json.dumps(polymarket.get('markets', []), indent=2)}

### Metaculus Forecasting Questions (Iran)
Status: {metaculus['status']}
Questions found: {len(metaculus.get('questions', []))}
Question data: {json.dumps(metaculus.get('questions', []), indent=2)}

### CENTCOM RSS Feed (Latest Releases)
Status: {centcom['status']}
Recent releases: {json.dumps(centcom.get('releases', []), indent=2)}
"""

    system_prompt = """You are an intelligence analyst producing a daily open-source intelligence (OSINT) briefing on the US military posture toward Iran. Write in IC (Intelligence Community) style with confidence levels.

Your output must be a JSON object with exactly these keys:
{
  "threat_level": "HIGH" or "CRITICAL" or "ELEVATED" or "ROUTINE",
  "threat_summary": "2-3 sentence summary explaining the threat level and why",
  "key_judgment": "IC-style key judgment paragraph with confidence level",
  "overnight_summary": "2-3 sentences: what changed in the last 24 hours — cover both force posture changes (new deployments, aircraft movements, repositioning) and diplomatic/military developments. Be specific about what is new vs unchanged.",
  "activity_groups": [
    {"title": "Group Title", "icon": "critical|notable|routine", "body": "Summary with [Source] tags"}
  ],
  "prediction_markets_summary": "2-3 sentence summary of what prediction markets are saying",
  "diplomatic_summary": "2-3 bullet points on diplomatic situation",
  "iw_updates": "Any updates to I&W indicators based on new data"
}

Use today's date: """ + date_str + """

Base your analysis on the live data provided AND your knowledge of the ongoing situation. If APIs returned errors, note that data was unavailable and rely on your existing knowledge. Always note that military aircraft frequently fly without transponders so OpenSky data is partial. Keep the language accessible to non-specialists while maintaining IC rigor."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 4096,
                "system": system_prompt,
                "messages": [
                    {"role": "user", "content": f"Here is today's collected data. Generate the daily briefing.\n\n{data_summary}"}
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        result = resp.json()

        text = result["content"][0]["text"]
        # Extract JSON from response (handle markdown code blocks)
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        analysis = json.loads(text.strip())
        print("[Claude] Analysis generated successfully")
        return analysis

    except Exception as e:
        print(f"[Claude] Error: {e}")
        return generate_fallback_analysis(opensky, polymarket, metaculus, centcom)


def generate_fallback_analysis(opensky, polymarket, metaculus, centcom):
    """Generate basic analysis without Claude API (for when key is missing)."""
    # Summarize prediction markets
    pm_markets = polymarket.get("markets", [])
    pm_summary = "Prediction market data unavailable."
    if pm_markets:
        top = pm_markets[0]
        pm_summary = f"Top Polymarket market: \"{top['question']}\" at {top['probability']}%."

    return {
        "threat_level": "HIGH",
        "threat_summary": f"US military posture in CENTCOM AOR remains elevated. OpenSky detected {opensky.get('mil_count', 'unknown')} possible military aircraft in the ME bounding box. Diplomatic talks are ongoing but no breakthrough reported. {pm_summary}",
        "key_judgment": "We assess with moderate confidence that the current US military buildup is designed to create credible strike options while maximizing diplomatic leverage. The force posture is sufficient for limited precision strikes if ordered, though key pre-strike indicators (CSAR forward-staging, NOTAMs, embassy evacuations) have not been publicly confirmed. [AUTO-GENERATED — Claude API key not configured]",
        "overnight_summary": "This is an automatically generated summary. No major changes in force posture detected in the past 24 hours. For full AI-powered analysis, add your ANTHROPIC_API_KEY to GitHub Secrets. Latest CENTCOM releases: " + "; ".join(centcom.get("releases", [])[:3]),
        "activity_groups": [
            {"title": "Data Collection Summary", "icon": "routine",
             "body": f"OpenSky: {opensky.get('mil_count', 0)} military aircraft detected. Polymarket: {len(pm_markets)} Iran markets tracked. Metaculus: {len(metaculus.get('questions', []))} questions found. CENTCOM: {len(centcom.get('releases', []))} releases. [Automated]"}
        ],
        "prediction_markets_summary": pm_summary,
        "diplomatic_summary": "Automated collection only. Add Claude API key for full analysis.",
        "iw_updates": "No automated I&W assessment available without Claude API.",
    }


# ──────────────────────────────────────────────────────────────
# HTML GENERATION
# ──────────────────────────────────────────────────────────────

def generate_html(analysis, opensky, polymarket, metaculus, centcom):
    """Generate the final HTML file with all data embedded."""
    print("[HTML] Generating page...")

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%A, %d %B %Y")
    time_str = now.strftime("%H:%M") + " GMT"

    # Format prediction markets for display
    markets_html = ""

    # Alert banner for unusual market movements
    market_alerts = polymarket.get("alerts", [])
    if market_alerts:
        alerts_inner = "".join(f'<div style="margin-bottom:6px">{a}</div>' for a in market_alerts)
        markets_html += f"""
        <div style="background:rgba(232,64,64,0.08);border:1px solid rgba(232,64,64,0.25);border-radius:4px;padding:14px 18px;margin-bottom:16px;font-size:12px;color:var(--accent-amber)">
          <div style="font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--accent-red);margin-bottom:8px">⚠ Unusual Market Activity Detected</div>
          {alerts_inner}
        </div>"""

    for m in polymarket.get("markets", []):
        prob = m["probability"]
        col = "var(--accent-red)" if prob >= 60 else "var(--accent-amber)" if prob >= 40 else "var(--text-secondary)"
        vol = float(m.get("volume", 0))
        vol_str = f"${vol/1e6:.0f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K" if vol >= 1e3 else f"${vol:.0f}"

        # Delta badge
        delta = m.get("prob_delta")
        delta_html = ""
        if delta is not None and delta != 0:
            arrow = "▲" if delta > 0 else "▼"
            dcol = "var(--accent-red)" if delta > 0 else "var(--accent-green)" if delta < 0 else "var(--text-muted)"
            # Highlight big moves
            if abs(delta) >= 5:
                delta_html = f'<span style="color:{dcol};font-weight:600;font-size:12px;margin-left:8px">{arrow}{abs(delta)}pts</span>'
            else:
                delta_html = f'<span style="color:{dcol};font-size:11px;margin-left:8px">{arrow}{abs(delta)}pts</span>'
        elif delta is not None and delta == 0:
            delta_html = '<span style="color:var(--text-muted);font-size:10px;margin-left:8px">unchanged</span>'

        # Volume spike indicator
        vol_ratio = m.get("vol_ratio")
        vol_badge = ""
        if vol_ratio and vol_ratio >= 3.0:
            vol_badge = f' · <span style="color:var(--accent-red)">🔺 {vol_ratio}x vol</span>'
        elif vol_ratio and vol_ratio >= 2.0:
            vol_badge = f' · <span style="color:var(--accent-amber)">{vol_ratio}x vol</span>'

        markets_html += f"""
        <div class="mrow">
          <div class="mq">{m['question']}<span class="mplat">Polymarket · Vol: {vol_str}{vol_badge}</span></div>
          <div class="mprob" style="color:{col}">{prob}%{delta_html}</div>
        </div>"""

    for q in metaculus.get("questions", []):
        prob = q["probability"]
        col = "var(--accent-red)" if prob >= 60 else "var(--accent-amber)" if prob >= 40 else "var(--text-secondary)"
        markets_html += f"""
        <div class="mrow">
          <div class="mq">{q['question']}<span class="mplat">Metaculus · {q.get('forecasters', '?')} forecasters</span></div>
          <div class="mprob" style="color:{col}">{prob}%</div>
        </div>"""

    if not markets_html:
        markets_html = '<div class="mrow"><div class="mq" style="color:var(--text-muted)">No prediction market data available this update. APIs may be temporarily unavailable.</div></div>'

    # Format activity groups
    groups_html = ""
    for g in analysis.get("activity_groups", []):
        groups_html += f"""
        <div class="activity-group">
          <div class="ag-header">
            <div class="ag-icon {g['icon']}"></div>
            <div class="ag-title">{g['title']}</div>
          </div>
          <div class="ag-body">{g['body']}</div>
        </div>"""

    # Format military aircraft as concise prose summary (not a table)
    mil_list = opensky.get("mil_aircraft", [])
    new_ac = [a for a in mil_list if a.get("status") == "new"]
    ret_ac = [a for a in mil_list if a.get("status") == "returning"]

    if mil_list:
        # Count by type
        type_counts = {}
        for a in mil_list:
            cs = a["callsign"].upper()
            if any(cs.startswith(p) for p in ["RCH","REACH","PACK","DUKE","MOOSE","FRED","CARGO","HERK"]):
                t = "airlift"
            elif any(cs.startswith(p) for p in ["ETHYL","JULIET","PEARL","STEEL","SHELL","TEAL"]):
                t = "tanker"
            elif any(cs.startswith(p) for p in ["HOMER","TOPCT","JAKE","TITAN","FORTE","MAGIC","SNTRY","REDEYE"]):
                t = "ISR/AWACS"
            elif any(cs.startswith(p) for p in ["DOOM","DEATH","BATT","MYTEE","BONE","VIPER","EAGLE","RAZOR"]):
                t = "strike"
            else:
                t = "other military"
            type_counts[t] = type_counts.get(t, 0) + 1

        type_str = ", ".join(f"{v} {k}" for k, v in sorted(type_counts.items(), key=lambda x: -x[1]))

        mil_html = f'<div style="font-size:13px;color:var(--text-secondary);line-height:1.7">'
        mil_html += f'Detected <strong style="color:var(--text-primary)">{len(mil_list)}</strong> military aircraft broadcasting ADS-B across the Middle East region: {type_str}.'

        if new_ac:
            # Build location-aware descriptions of new aircraft with airframe IDs
            new_descs = []
            for a in new_ac[:6]:
                loc = a.get("location_desc", "unknown")
                airframe = a.get("airframe", "")
                if airframe and airframe != "Unknown type":
                    new_descs.append(f'{a["callsign"]} — {airframe} ({loc})')
                else:
                    new_descs.append(f'{a["callsign"]} ({loc})')
            new_detail = "; ".join(new_descs)
            more = f" and {len(new_ac) - 6} more" if len(new_ac) > 6 else ""
            mil_html += f' Of these, <strong style="color:var(--accent-cyan)">{len(new_ac)} are new</strong> since the last scan: {new_detail}{more}.'
        if ret_ac:
            mil_html += f' {len(ret_ac)} were already present in yesterday\'s scan.'

        mil_html += '</div>'
    else:
        mil_html = '<div style="font-size:13px;color:var(--text-muted);line-height:1.7">No military aircraft with active transponders detected in this scan. Most military flights do not broadcast ADS-B — absence of detections does not mean absence of activity.</div>'

    # Format CENTCOM releases
    centcom_html = ""
    for r in centcom.get("releases", [])[:8]:
        centcom_html += f'<li class="sli"><span class="sli-dot" style="background:var(--accent-blue)"></span><span>{r}</span></li>'

    # Feed statuses
    feeds_data = [
        ("OpenSky Network", opensky["status"], f"{opensky.get('total_aircraft', 0)} aircraft / {opensky.get('mil_count', 0)} military", "OAuth2 authenticated" if OPENSKY_CLIENT_ID else "Anonymous (add OPENSKY_CLIENT_ID)"),
        ("Polymarket Gamma", polymarket["status"], f"{len(polymarket.get('markets', []))} Iran markets", "Free — no auth required"),
        ("Metaculus", metaculus["status"], f"{len(metaculus.get('questions', []))} questions", "Free — no auth required"),
        ("CENTCOM RSS", centcom["status"], f"{len(centcom.get('releases', []))} releases", "Official DoD feed"),
        ("Claude Analysis", "ok" if ANTHROPIC_API_KEY else "warn", "Haiku 4.5 (~$0.02/update)" if ANTHROPIC_API_KEY else "No API key — using fallback", "Requires ANTHROPIC_API_KEY"),
    ]
    feeds_html = ""
    for name, status, detail, note in feeds_data:
        dot_cls = "ok" if status == "ok" else "warn" if status in ("warn", "error") else "off"
        feeds_html += f"""
        <div class="feed-item">
          <div class="feed-dot {dot_cls}"></div>
          <div><div class="feed-label">{name}</div><div class="feed-detail">{detail}</div><div class="feed-detail">{note}</div></div>
        </div>"""

    threat_level = analysis.get("threat_level", "HIGH")
    tl_lower = threat_level.lower()

    # Read template and fill
    html = HTML_TEMPLATE.replace("{{DATE_STR}}", date_str)
    html = html.replace("{{TIME_STR}}", time_str)
    html = html.replace("{{THREAT_LEVEL}}", threat_level)
    html = html.replace("{{THREAT_LEVEL_LOWER}}", tl_lower)
    # Helper: Claude sometimes returns lists instead of strings
    def s(val):
        if isinstance(val, list):
            return "<br>".join(str(item) for item in val)
        return str(val) if val else ""

    html = html.replace("{{THREAT_SUMMARY}}", s(analysis.get("threat_summary", "")))
    html = html.replace("{{KEY_JUDGMENT}}", s(analysis.get("key_judgment", "")))
    html = html.replace("{{OVERNIGHT_SUMMARY}}", s(analysis.get("overnight_summary", "")))
    html = html.replace("{{ACTIVITY_GROUPS}}", groups_html)
    html = html.replace("{{MARKETS_HTML}}", markets_html)
    html = html.replace("{{MARKETS_SUMMARY}}", s(analysis.get("prediction_markets_summary", "")))
    html = html.replace("{{MIL_AIRCRAFT_HTML}}", mil_html)
    html = html.replace("{{MIL_COUNT}}", str(opensky.get("mil_count", 0)))
    html = html.replace("{{TOTAL_AIRCRAFT}}", str(opensky.get("total_aircraft", 0)))

    # Inject aircraft data as JSON for the map
    ac_for_map = []
    new_ac_count = 0
    for a in opensky.get("mil_aircraft", []):
        ac_for_map.append({
            "callsign": a.get("callsign", ""),
            "lat": a.get("lat"),
            "lon": a.get("lon"),
            "alt_ft": a.get("alt_ft"),
            "origin": a.get("origin", ""),
            "status": a.get("status", "new"),
            "location_desc": a.get("location_desc", ""),
            "airframe": a.get("airframe", ""),
            "role": a.get("role", ""),
        })
        if a.get("status") == "new":
            new_ac_count += 1
    html = html.replace("{{AIRCRAFT_JSON}}", json.dumps(ac_for_map))
    html = html.replace("{{NEW_AC_COUNT}}", str(new_ac_count))

    html = html.replace("{{FEEDS_HTML}}", feeds_html)
    html = html.replace("{{CENTCOM_HTML}}", centcom_html)
    html = html.replace("{{DIPLOMATIC_SUMMARY}}", s(analysis.get("diplomatic_summary", "")))
    html = html.replace("{{IW_UPDATES}}", s(analysis.get("iw_updates", "")))

    return html


# ──────────────────────────────────────────────────────────────
# HTML TEMPLATE (embedded as string to keep single-file simplicity)
# ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IRAN WATCH — OSINT Force Posture Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600;700&family=IBM+Plex+Sans:wght@300;400;500;600;700&family=IBM+Plex+Serif:ital,wght@0,400;1,400&display=swap" rel="stylesheet">
<style>
:root{--bg-primary:#0a0c10;--bg-card:#11141b;--bg-card-hover:#161a24;--bg-elevated:#1a1f2b;--border:#1e2433;--border-accent:#2a3148;--text-primary:#e8eaf0;--text-secondary:#8b93a8;--text-muted:#565e73;--accent-red:#e84040;--accent-red-dim:#5c1a1a;--accent-amber:#e8a020;--accent-amber-dim:#5c4110;--accent-green:#30c060;--accent-blue:#4088e8;--accent-blue-dim:#1a3860;--accent-cyan:#40c8e8}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg-primary);color:var(--text-primary);font-family:'IBM Plex Sans',sans-serif;font-size:14px;line-height:1.6;padding-bottom:44px}
header{border-bottom:1px solid var(--border);padding:20px 32px;display:flex;align-items:center;justify-content:space-between;background:linear-gradient(180deg,#0e1118,var(--bg-primary));position:sticky;top:0;z-index:100;backdrop-filter:blur(12px)}
.hdr-left{display:flex;align-items:center;gap:16px}
.logo{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:18px;letter-spacing:4px;text-transform:uppercase;display:flex;align-items:center;gap:10px}
.logo-dot{width:8px;height:8px;border-radius:50%;background:var(--accent-red);box-shadow:0 0 8px var(--accent-red),0 0 20px rgba(232,64,64,0.3);animation:glow 2s ease-in-out infinite}
@keyframes glow{0%,100%{box-shadow:0 0 8px var(--accent-red),0 0 20px rgba(232,64,64,0.3)}50%{box-shadow:0 0 12px var(--accent-red),0 0 30px rgba(232,64,64,0.5)}}
.subtitle{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-muted);letter-spacing:2px;text-transform:uppercase}
.ts{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-muted);text-align:right}
.ts strong{color:var(--text-secondary);display:block}
.wrap{max-width:960px;margin:0 auto;padding:24px 32px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:28px}
.full{grid-column:1/-1}
.sec{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:3px;text-transform:uppercase;color:var(--text-muted);margin:32px 0 16px;padding-bottom:8px;border-bottom:1px solid var(--border)}
.threat{border-radius:6px;padding:20px 28px;margin-bottom:28px;display:flex;align-items:flex-start;gap:20px}
.threat.high{background:linear-gradient(135deg,var(--accent-amber-dim),rgba(232,160,32,0.05));border:1px solid rgba(232,160,32,0.3)}
.threat.critical{background:linear-gradient(135deg,var(--accent-red-dim),rgba(232,64,64,0.05));border:1px solid rgba(232,64,64,0.3)}
.threat.elevated{background:linear-gradient(135deg,var(--accent-blue-dim),rgba(64,136,232,0.05));border:1px solid rgba(64,136,232,0.3)}
.threat.routine{background:var(--bg-card);border:1px solid var(--border)}
.tlevel{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:13px;letter-spacing:2px;text-transform:uppercase;padding:8px 16px;border-radius:4px;white-space:nowrap;flex-shrink:0}
.tlevel.high{background:var(--accent-amber);color:#000}.tlevel.critical{background:var(--accent-red);color:#fff}.tlevel.elevated{background:var(--accent-blue);color:#fff}.tlevel.routine{background:var(--accent-green);color:#000}
.tsummary{font-size:14px;line-height:1.6}
.tscale{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted);margin-top:10px;line-height:1.8;letter-spacing:.5px}
.tscale span{padding:2px 6px;border-radius:2px;margin-right:4px}
.card{background:var(--bg-card);border:1px solid var(--border);border-radius:6px;overflow:hidden}
.ch{display:flex;align-items:center;justify-content:space-between;padding:16px 20px 12px;border-bottom:1px solid var(--border)}
.ct{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--text-secondary)}
.badge{font-family:'IBM Plex Mono',monospace;font-size:10px;padding:3px 8px;border-radius:3px;letter-spacing:1px;text-transform:uppercase}
.badge.live{background:rgba(232,64,64,0.15);color:var(--accent-red);border:1px solid rgba(232,64,64,0.3)}
.badge.ok{background:rgba(48,192,96,0.15);color:var(--accent-green);border:1px solid rgba(48,192,96,0.3)}
.badge.warn{background:rgba(232,160,32,0.15);color:var(--accent-amber);border:1px solid rgba(232,160,32,0.3)}
.cb{padding:16px 20px 20px}
table.pt{width:100%;border-collapse:collapse}
.pt th{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-muted);text-align:left;padding:8px 12px;border-bottom:1px solid var(--border)}
.pt td{padding:10px 12px;font-size:13px;border-bottom:1px solid rgba(30,36,51,0.5);vertical-align:top}
.pt tr:last-child td{border-bottom:none}
.aname{font-weight:500}.adet{font-size:12px;color:var(--text-secondary);margin-top:2px}
.loc{font-family:'IBM Plex Mono',monospace;font-size:12px;color:var(--text-secondary)}
.abox{background:var(--bg-elevated);border-radius:4px;padding:20px;margin-bottom:16px}
.abox:last-child{margin-bottom:0}
.alabel{font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:2px;text-transform:uppercase;margin-bottom:8px}
.alabel.amber{color:var(--accent-amber)}.alabel.blue{color:var(--accent-blue)}.alabel.red{color:var(--accent-red)}
.atext{font-family:'IBM Plex Serif',serif;font-size:14px;line-height:1.7}
.atext em{color:var(--accent-amber);font-style:italic}
.conf{display:inline-block;font-family:'IBM Plex Mono',monospace;font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;padding:3px 8px;border-radius:3px;margin-top:8px}
.conf.mod{background:rgba(232,160,32,0.12);color:var(--accent-amber)}
.activity-group{padding:16px 0;border-bottom:1px solid rgba(30,36,51,0.5)}
.activity-group:last-child{border-bottom:none}
.ag-header{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.ag-icon{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.ag-icon.critical{background:var(--accent-red);box-shadow:0 0 6px rgba(232,64,64,0.4)}
.ag-icon.notable{background:var(--accent-amber)}.ag-icon.routine{background:var(--accent-blue)}
.ag-title{font-family:'IBM Plex Mono',monospace;font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--text-primary)}
.ag-body{font-size:13px;color:var(--text-secondary);line-height:1.6;margin-left:18px}
.mrow{display:flex;align-items:center;justify-content:space-between;padding:12px 0;border-bottom:1px solid rgba(30,36,51,0.5)}
.mrow:last-child{border-bottom:none}
.mq{font-size:13px;flex:1;padding-right:16px}
.mplat{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted);display:block;margin-top:2px}
.mprob{font-family:'IBM Plex Mono',monospace;font-weight:700;font-size:20px;min-width:64px;text-align:right}
.feeds-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.feed-item{background:var(--bg-elevated);border-radius:4px;padding:10px 14px;display:flex;align-items:center;gap:10px}
.feed-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.feed-dot.ok{background:var(--accent-green)}.feed-dot.warn{background:var(--accent-amber)}.feed-dot.off{background:var(--text-muted)}
.feed-label{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-secondary)}
.feed-detail{font-size:10px;color:var(--text-muted)}
.slist{list-style:none}
.sli{padding:10px 0;border-bottom:1px solid rgba(30,36,51,0.5);display:flex;gap:10px;align-items:flex-start;font-size:13px}
.sli:last-child{border-bottom:none}
.sli-dot{width:6px;height:6px;border-radius:50%;margin-top:7px;flex-shrink:0;background:var(--accent-blue)}
.srcs{display:flex;flex-wrap:wrap;gap:8px;padding:12px 20px;border-top:1px solid var(--border)}
.stag{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted);background:rgba(30,36,51,0.5);padding:4px 10px;border-radius:3px}
.stag a{color:var(--text-secondary);text-decoration:none}
.bar{position:fixed;bottom:0;left:0;right:0;background:var(--bg-card);border-top:1px solid var(--border);padding:8px 32px;display:flex;align-items:center;justify-content:space-between;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted);z-index:100}
@media(max-width:900px){.grid{grid-template-columns:1fr}header{padding:16px;flex-wrap:wrap;gap:12px}.wrap{padding:16px}}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:var(--bg-primary)}::-webkit-scrollbar-thumb{background:var(--border-accent);border-radius:3px}
.how-btn{font-family:'IBM Plex Mono',monospace;font-size:11px;letter-spacing:1.5px;text-transform:uppercase;color:var(--text-secondary);background:rgba(30,36,51,0.6);border:1px solid var(--border);border-radius:4px;padding:7px 14px;cursor:pointer;transition:all 0.2s;display:inline-flex;align-items:center;gap:6px}
.how-btn:hover{color:var(--accent-cyan);border-color:var(--accent-cyan);background:rgba(64,200,232,0.06)}
.how-btn svg{flex-shrink:0}
.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,0.7);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;z-index:1000;opacity:0;pointer-events:none;transition:opacity 0.2s}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;max-width:560px;width:90%;transform:translateY(10px);transition:transform 0.2s}
.modal-overlay.open .modal{transform:translateY(0)}
.modal-header{display:flex;align-items:center;justify-content:space-between;padding:18px 24px;border-bottom:1px solid var(--border)}
.modal-title{font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--accent-cyan);display:flex;align-items:center;gap:8px}
.modal-close{background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px;padding:4px 8px;border-radius:3px}
.modal-close:hover{color:var(--text-primary);background:var(--bg-elevated)}
.modal-body{padding:20px 24px 24px}
.modal-body p{font-size:13.5px;line-height:1.7;color:var(--text-secondary);margin-bottom:16px}
.modal-body p:last-child{margin-bottom:0}
.source-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:16px 0}
.source-item{background:var(--bg-elevated);border-radius:4px;padding:10px 12px;display:flex;align-items:center;gap:8px}
.source-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.source-name{font-family:'IBM Plex Mono',monospace;font-size:11px;color:var(--text-secondary)}
.source-what{font-size:10px;color:var(--text-muted)}
.modal-footer{font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted);padding:12px 24px;border-top:1px solid var(--border);text-align:center;letter-spacing:0.5px}
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <div class="logo"><span class="logo-dot"></span>IRAN WATCH</div>
    <span class="subtitle">Open-Source Force Posture Monitor</span>
    <button class="how-btn" onclick="document.getElementById('howModal').classList.add('open')"><svg viewBox="0 0 24 24" width="14" height="14" stroke="currentColor" fill="none" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>How do I work?</button>
  </div>
  <div class="ts"><strong>{{DATE_STR}} · {{TIME_STR}}</strong>Auto-updates daily at 0500 GMT</div>
</header>
<div class="modal-overlay" id="howModal" onclick="if(event.target===this)this.classList.remove('open')">
<div class="modal">
<div class="modal-header"><div class="modal-title"><svg viewBox="0 0 24 24" width="16" height="16" stroke="currentColor" fill="none" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 015.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>How Iran Watch Works</div><button class="modal-close" onclick="document.getElementById('howModal').classList.remove('open')">&#10005;</button></div>
<div class="modal-body">
<p>Every morning at <strong style="color:var(--text-primary)">5:00 AM GMT</strong>, a script automatically collects data from public sources, sends it to an AI model for analysis, and publishes this page. No human edits the content — it's generated fresh each day.</p>
<div class="source-grid">
<div class="source-item"><div class="source-dot" style="background:var(--accent-cyan)"></div><div><div class="source-name">OpenSky Network</div><div class="source-what">Military aircraft tracking</div></div></div>
<div class="source-item"><div class="source-dot" style="background:var(--accent-amber)"></div><div><div class="source-name">Polymarket</div><div class="source-what">Prediction market odds</div></div></div>
<div class="source-item"><div class="source-dot" style="background:var(--accent-blue)"></div><div><div class="source-name">Metaculus</div><div class="source-what">Forecaster consensus</div></div></div>
<div class="source-item"><div class="source-dot" style="background:var(--accent-red)"></div><div><div class="source-name">CENTCOM / DoD</div><div class="source-what">Official military releases</div></div></div>
</div>
<p>The AI reads this data and produces an intelligence-style briefing: a <strong style="color:var(--text-primary)">threat level</strong>, a <strong style="color:var(--text-primary)">key judgment</strong> with confidence level, a summary of <strong style="color:var(--text-primary)">what changed overnight</strong>, and a snapshot of where <strong style="color:var(--text-primary)">prediction markets</strong> stand.</p>
<p style="color:var(--text-muted);font-size:12px">&#9888; This is an automated OSINT tool, not professional intelligence. Military aircraft often fly without transponders. Prediction markets reflect betting sentiment, not ground truth. Always cross-reference.</p>
</div>
<div class="modal-footer">Built with OpenSky · Polymarket · Claude AI · GitHub Actions</div>
</div>
</div>
<main class="wrap">

<div class="threat {{THREAT_LEVEL_LOWER}}">
  <div style="flex-shrink:0"><div class="tlevel {{THREAT_LEVEL_LOWER}}">{{THREAT_LEVEL}}</div></div>
  <div>
    <div class="tsummary">{{THREAT_SUMMARY}}</div>
    <div class="tscale">
      <strong>SCALE:</strong>
      <span style="background:var(--accent-red);color:#fff">CRITICAL</span> Strike imminent (hours–days)
      <span style="background:var(--accent-amber);color:#000">HIGH</span> Forces sufficient; buildup active
      <span style="background:var(--accent-blue);color:#fff">ELEVATED</span> Above baseline; gaps remain
      <span style="background:rgba(48,192,96,0.8);color:#000">ROUTINE</span> Normal standing posture
    </div>
  </div>
</div>

<div class="sec">Bottom Line Up Front</div>
<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">Daily Assessment — {{DATE_STR}}</span><span class="badge warn">IC-STYLE</span></div>
  <div class="cb">
    <div class="abox" style="border-left:3px solid var(--accent-amber)">
      <div class="alabel amber">Key Judgment</div>
      <div class="atext">{{KEY_JUDGMENT}}<div class="conf mod">Moderate Confidence</div></div>
    </div>
    <div class="abox" style="border-left:3px solid var(--accent-blue)">
      <div class="alabel blue">What Changed — Last 24 Hours</div>
      <div class="atext">{{OVERNIGHT_SUMMARY}}</div>
    </div>
  </div>
</div>

<div class="sec">Significant Activity — Grouped</div>
<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">Activity Summary</span><span class="badge live">Today</span></div>
  <div class="cb">{{ACTIVITY_GROUPS}}</div>
</div>

<div class="sec">Live Aircraft Detection — OpenSky Network</div>
<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">✈ Military Aircraft in ME Airspace</span><span class="badge ok">{{MIL_COUNT}} DETECTED / {{TOTAL_AIRCRAFT}} TOTAL</span></div>
  <div class="cb">
    {{MIL_AIRCRAFT_HTML}}
    <div style="margin-top:12px;font-size:11px;color:var(--text-muted)">⚠ Most military aircraft fly without ADS-B transponders. This represents only the fraction that broadcast.</div>
  </div>
  <div class="srcs"><span class="stag"><a href="https://opensky-network.org" target="_blank">OpenSky Network API</a></span><span class="stag">Free, rate-limited</span></div>
</div>

<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">New Military Activity — Last 24 Hours</span><span class="badge warn">{{NEW_AC_COUNT}} NEW</span></div>
  <div style="position:relative;width:100%;height:500px;background:#0d1117;overflow:hidden" id="mapWrap">
    <canvas id="mapCanvas"></canvas>
    <div id="tooltip" style="position:absolute;background:#1a1f2b;border:1px solid #2a3148;border-radius:4px;padding:10px 14px;font-family:'IBM Plex Mono',monospace;font-size:11px;pointer-events:none;opacity:0;transition:opacity 0.15s;z-index:10;max-width:220px;box-shadow:0 4px 16px rgba(0,0,0,0.5)"></div>
  </div>
  <div style="display:flex;gap:16px;padding:12px 20px;border-top:1px solid var(--border);flex-wrap:wrap">
    <div style="display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted)"><div style="width:10px;height:10px;border-radius:50%;background:#e84040;box-shadow:0 0 6px #e84040"></div>NEW — Airlift</div>
    <div style="display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted)"><div style="width:10px;height:10px;border-radius:50%;background:#4088e8;box-shadow:0 0 6px #4088e8"></div>NEW — Tanker</div>
    <div style="display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted)"><div style="width:10px;height:10px;border-radius:50%;background:#e8a020;box-shadow:0 0 6px #e8a020"></div>NEW — ISR/AWACS</div>
    <div style="display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted)"><div style="width:10px;height:10px;border-radius:50%;background:#30c060;box-shadow:0 0 6px #30c060"></div>NEW — Strike</div>
    <div style="display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted)"><div style="width:10px;height:10px;border-radius:50%;background:#3a4158;border:1px solid #565e73"></div>Still present (seen yesterday)</div>
    <div style="display:flex;align-items:center;gap:6px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:var(--text-muted)"><svg width="12" height="12"><rect x="1" y="1" width="10" height="10" fill="none" stroke="#565e73" stroke-width="1" stroke-dasharray="2 2"/></svg>US/Coalition Base</div>
  </div>
  <div style="font-size:11px;color:var(--text-muted);padding:8px 20px 12px;border-top:1px solid rgba(30,36,51,0.5)">⚠ Bright markers = aircraft NOT seen in previous scan (new arrivals). Dim markers = still present from yesterday. Most military flights do not broadcast ADS-B — this is a partial picture.</div>
</div>

<script>
(function(){
const aircraft = {{AIRCRAFT_JSON}};
const bases = [
  {name:"Al Udeid AB",lat:25.117,lon:51.315},{name:"Al Dhafra AB",lat:24.248,lon:54.547},
  {name:"Ali Al Salem AB",lat:29.346,lon:47.521},{name:"Muwaffaq Salti AB",lat:32.356,lon:36.259},
  {name:"Prince Sultan AB",lat:24.062,lon:47.580},{name:"Camp Lemonnier",lat:11.547,lon:43.155},
  {name:"Incirlik AB",lat:37.002,lon:35.426},{name:"RAF Akrotiri",lat:34.590,lon:32.988},
  {name:"NSA Bahrain",lat:26.236,lon:50.577}
];
const borders={"Iran":[[25.1,61.6],[25.3,58.9],[26.3,56.3],[27.2,54.7],[26.5,53.4],[27.0,51.5],[29.8,50.3],[30.4,48.8],[31.0,47.7],[32.3,47.4],[33.7,46.0],[35.1,45.4],[36.6,45.0],[37.4,44.8],[38.3,44.4],[39.4,44.0],[39.8,47.8],[39.3,48.0],[38.9,48.9],[37.6,49.1],[37.3,50.1],[36.7,53.9],[37.4,55.4],[37.3,57.2],[35.8,60.5],[34.5,60.9],[33.7,60.5],[31.3,61.7],[27.2,63.3],[25.1,61.6]],"Iraq":[[29.1,47.4],[30.4,47.0],[31.0,47.7],[32.3,47.4],[33.7,46.0],[35.1,45.4],[36.6,45.0],[37.4,44.8],[37.1,42.4],[36.8,41.0],[33.4,40.9],[32.0,39.0],[30.0,40.0],[29.1,44.7],[29.1,47.4]],"Saudi Arabia":[[16.4,42.7],[17.5,43.4],[18.2,44.2],[19.0,45.0],[20.0,45.0],[21.5,49.0],[22.5,50.8],[24.0,52.0],[24.2,51.6],[25.8,50.8],[27.0,49.6],[28.5,48.4],[29.1,47.4],[29.1,44.7],[28.0,37.0],[25.0,37.5],[20.0,40.0],[17.8,42.0],[16.4,42.7]],"UAE":[[24.0,52.0],[24.2,53.5],[24.2,54.5],[25.6,56.3],[26.1,56.0],[24.9,55.8],[24.3,55.5],[24.0,52.0]],"Oman":[[16.6,53.0],[17.0,55.0],[20.0,57.5],[21.5,59.8],[22.8,59.8],[23.6,58.5],[25.3,57.0],[26.3,56.3],[25.6,56.3],[24.2,54.5],[24.2,53.5],[24.0,52.0],[22.5,55.1],[20.0,55.8],[16.6,53.0]],"Kuwait":[[28.5,48.4],[29.1,47.4],[30.1,47.7],[29.9,48.4],[29.4,48.4],[28.5,48.4]],"Qatar":[[24.5,50.8],[25.3,50.7],[26.2,51.2],[26.1,51.6],[25.4,51.6],[24.5,51.3],[24.5,50.8]],"Turkey":[[36.0,36.0],[36.2,33.0],[36.8,30.6],[37.0,28.0],[38.4,26.2],[40.0,26.0],[41.0,28.8],[42.0,33.4],[41.5,36.4],[42.5,43.5],[41.2,43.5],[40.6,44.0],[39.8,44.5],[38.3,44.4],[37.4,44.8],[37.1,42.4],[36.8,41.0],[37.0,38.0],[36.7,37.0],[36.0,36.0]],"Syria":[[32.3,35.8],[33.0,35.9],[34.7,35.8],[35.5,36.0],[36.0,36.0],[36.7,37.0],[37.0,38.0],[36.8,41.0],[33.4,40.9],[32.0,39.0],[32.3,35.8]],"Jordan":[[29.1,34.9],[29.5,35.0],[31.5,35.5],[32.3,35.8],[32.0,39.0],[30.0,40.0],[29.1,36.0],[29.1,34.9]],"Israel":[[29.5,34.9],[31.3,34.3],[32.5,34.9],[33.3,35.6],[32.3,35.8],[31.5,35.5],[29.5,35.0],[29.5,34.9]],"Egypt":[[22.0,25.0],[22.0,36.9],[29.5,34.9],[31.3,34.3],[31.5,32.0],[30.8,29.0],[31.5,25.0],[22.0,25.0]],"Yemen":[[12.6,43.3],[13.0,45.0],[14.0,47.0],[15.5,52.2],[16.6,53.0],[20.0,55.8],[19.0,52.0],[18.2,50.0],[17.5,49.0],[16.5,47.5],[16.0,44.5],[13.0,43.4],[12.6,43.3]],"Pakistan":[[25.1,61.6],[25.2,63.5],[25.6,64.7],[26.5,66.0],[27.5,67.0],[28.2,68.5],[30.0,66.5],[31.0,67.0],[33.0,69.5],[35.5,71.0],[37.0,71.5],[37.1,67.8],[33.7,60.5],[31.3,61.7],[27.2,63.3],[25.1,61.6]],"Afghanistan":[[29.4,64.0],[30.5,62.0],[33.7,60.5],[37.1,67.8],[37.0,71.5],[35.5,71.0],[33.0,69.5],[31.0,67.0],[30.0,66.5],[29.4,64.0]],"Somalia":[[11.5,43.2],[12.0,44.0],[11.5,49.0],[10.0,51.0],[5.0,48.0],[1.6,41.6],[4.0,42.0],[8.0,44.0],[11.5,43.2]],"Eritrea":[[12.6,43.3],[13.0,42.4],[15.0,39.5],[18.0,38.5],[18.0,40.0],[15.5,40.5],[13.0,43.0],[12.6,43.3]],"Djibouti":[[11.0,41.8],[11.5,43.2],[12.7,43.3],[12.0,42.4],[11.0,41.8]]};
const labels=[[32,"IRAN",53],[33,"IRAQ",43.5],[24,"SAUDI ARABIA",45],[35,"TURKEY",35],[34,"SYRIA",38],[25,"UAE",54.5],[25.5,"QATAR",51.2],[31,"JORDAN",37],[15,"YEMEN",47],[28,"OMAN",57],[14,"SOMALIA",46],[33,"AFG",66],[30,"PAK",65],[27,"EGYPT",30]];
const VIEW={cenLat:27,cenLon:48,scale:14};
const wrap=document.getElementById('mapWrap');
const canvas=document.getElementById('mapCanvas');
const ctx=canvas.getContext('2d');
const tip=document.getElementById('tooltip');
let W,H;
function toX(lon){return(lon-VIEW.cenLon)*VIEW.scale+W/2}
function toY(lat){return(VIEW.cenLat-lat)*VIEW.scale+H/2}
function getCat(cs){
  cs=cs.toUpperCase();
  const c=[
    {p:['RCH','REACH','PACK','DUKE','MOOSE','FRED','CARGO','HERK'],t:'Airlift',c:'#e84040'},
    {p:['ETHYL','JULIET','PEARL','STEEL','SHELL','TEAL','NKAC','PKSN'],t:'Tanker',c:'#4088e8'},
    {p:['HOMER','TOPCT','JAKE','TITAN','FORTE','MAGIC','SNTRY','REDEYE','OLIVE','MAZDA'],t:'ISR/AWACS',c:'#e8a020'},
    {p:['DOOM','DEATH','BATT','MYTEE','BONE','VIPER','EAGLE','RAZOR','HAWK','STRIKE','WRATH','BOLT','ASCOT'],t:'Strike',c:'#30c060'}
  ];
  for(const g of c)for(const px of g.p)if(cs.startsWith(px))return g;
  return{t:'Military',c:'#8b93a8'};
}
function draw(){
  ctx.clearRect(0,0,W,H);ctx.fillStyle='#0d1117';ctx.fillRect(0,0,W,H);
  // Grid
  ctx.strokeStyle='rgba(30,36,51,0.5)';ctx.lineWidth=0.5;
  for(let lat=-10;lat<=50;lat+=5){ctx.beginPath();ctx.moveTo(toX(10),toY(lat));ctx.lineTo(toX(85),toY(lat));ctx.stroke()}
  for(let lon=10;lon<=85;lon+=5){ctx.beginPath();ctx.moveTo(toX(lon),toY(-10));ctx.lineTo(toX(lon),toY(50));ctx.stroke()}
  // Borders
  ctx.lineWidth=1;ctx.fillStyle='rgba(26,31,43,0.6)';
  for(const[name,pts]of Object.entries(borders)){
    ctx.strokeStyle=name==='Iran'?'rgba(232,64,64,0.25)':'#2a3148';
    ctx.lineWidth=name==='Iran'?2:1;
    ctx.beginPath();pts.forEach((p,i)=>{const x=toX(p[1]),y=toY(p[0]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});ctx.closePath();ctx.fill();ctx.stroke();
  }
  // Labels
  ctx.font='9px IBM Plex Mono,monospace';ctx.fillStyle='#3a4158';ctx.textAlign='center';
  labels.forEach(([lat,text,lon])=>{const lines=text.split(' ');if(lines.length>1){lines.forEach((l,i)=>ctx.fillText(l,toX(lon),toY(lat)+i*11))}else{ctx.fillText(text,toX(lon),toY(lat))}});
  // Bases
  ctx.setLineDash([3,3]);ctx.strokeStyle='#565e73';ctx.lineWidth=1;
  bases.forEach(b=>{const x=toX(b.lon),y=toY(b.lat);ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.stroke();ctx.font='8px IBM Plex Mono,monospace';ctx.fillStyle='#565e73';ctx.textAlign='left';ctx.fillText(b.name,x+8,y+3)});
  ctx.setLineDash([]);
  // Aircraft — returning (dim) first, then new (bright) on top
  const returning=aircraft.filter(a=>a.status==='returning');
  const newAc=aircraft.filter(a=>a.status==='new');
  returning.forEach(ac=>{
    const x=toX(ac.lon),y=toY(ac.lat);
    ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fillStyle='#3a4158';ctx.fill();ctx.strokeStyle='#565e73';ctx.lineWidth=1;ctx.stroke();
    ctx.font='8px IBM Plex Mono,monospace';ctx.fillStyle='#565e73';ctx.textAlign='left';ctx.fillText(ac.callsign,x+8,y+3);
  });
  newAc.forEach(ac=>{
    const x=toX(ac.lon),y=toY(ac.lat);const cat=getCat(ac.callsign);
    // Glow
    const g=ctx.createRadialGradient(x,y,0,x,y,20);g.addColorStop(0,cat.c+'40');g.addColorStop(1,cat.c+'00');ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,20,0,Math.PI*2);ctx.fill();
    // Dot
    ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fillStyle=cat.c+'90';ctx.fill();ctx.strokeStyle=cat.c;ctx.lineWidth=2;ctx.stroke();
    // Label
    ctx.font='bold 9px IBM Plex Mono,monospace';ctx.fillStyle=cat.c;ctx.textAlign='left';ctx.fillText(ac.callsign,x+10,y-4);ctx.font='8px IBM Plex Mono,monospace';ctx.fillStyle='#8b93a8';ctx.fillText(cat.t,x+10,y+7);
  });
}
function resize(){W=wrap.clientWidth;H=wrap.clientHeight;canvas.width=W*devicePixelRatio;canvas.height=H*devicePixelRatio;canvas.style.width=W+'px';canvas.style.height=H+'px';ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);draw()}
resize();window.addEventListener('resize',resize);
// Tooltip
let hov=null;
wrap.addEventListener('mousemove',e=>{
  const r=canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;let found=null;
  for(const ac of aircraft){if(Math.hypot(mx-toX(ac.lon),my-toY(ac.lat))<16){found=ac;break}}
  if(!found)for(const b of bases){if(Math.hypot(mx-toX(b.lon),my-toY(b.lat))<12){found={callsign:b.name,origin:'',alt_ft:null,status:'base'};break}}
  if(found&&found!==hov){hov=found;const cat=found.status==='base'?{t:'US/Coalition Base',c:'#565e73'}:getCat(found.callsign);const st=found.status==='new'?'<span style="color:#40c8e8">★ NEW — not seen yesterday</span>':found.status==='returning'?'<span style="color:#565e73">Still present from yesterday</span>':'';let h='<div style="font-weight:600;color:#40c8e8;font-size:12px">'+found.callsign+'</div>';if(found.airframe)h+='<div style="color:#e8eaf0;margin-top:3px;font-size:11px;font-weight:500">'+found.airframe+'</div>';if(found.role)h+='<div style="color:#8b93a8;font-size:10px">'+found.role+'</div>';if(found.location_desc)h+='<div style="color:#8b93a8;margin-top:3px;font-size:10px">📍 '+found.location_desc+'</div>';if(found.alt_ft)h+='<div style="color:#565e73;font-size:10px">'+found.alt_ft.toLocaleString()+' ft · '+found.origin+'</div>';h+='<div style="display:inline-block;padding:2px 6px;border-radius:2px;font-size:9px;margin-top:5px;background:'+cat.c+'20;color:'+cat.c+'">'+cat.t+'</div>';if(st)h+='<div style="margin-top:4px;font-size:9px">'+st+'</div>';tip.innerHTML=h;tip.style.left=Math.min(mx+16,W-220)+'px';tip.style.top=(my-10)+'px';tip.style.opacity='1'}else if(!found){hov=null;tip.style.opacity='0'}
});
wrap.addEventListener('mouseleave',()=>{hov=null;tip.style.opacity='0'});
})();
</script>

<div class="sec">Forecasting Panel — Prediction Markets</div>
<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">Market Consensus</span><span class="badge ok">Live Data</span></div>
  <div class="cb">
    <div style="font-size:13px;color:var(--text-secondary);margin-bottom:12px;line-height:1.5">{{MARKETS_SUMMARY}}</div>
    {{MARKETS_HTML}}
  </div>
  <div class="srcs">
    <span class="stag"><a href="https://polymarket.com/predictions/iran" target="_blank">Polymarket</a></span>
    <span class="stag"><a href="https://www.metaculus.com/questions/" target="_blank">Metaculus</a></span>
    <span class="stag"><a href="https://kalshi.com" target="_blank">Kalshi</a></span>
    <span class="stag"><a href="https://manifold.markets" target="_blank">Manifold</a></span>
  </div>
</div>

<div class="sec">Live Data Feed Status</div>
<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">📡 API Connections</span></div>
  <div class="cb"><div class="feeds-grid">{{FEEDS_HTML}}</div></div>
</div>

<div class="sec">CENTCOM Official Releases</div>
<div class="card full" style="margin-bottom:28px">
  <div class="ch"><span class="ct">📡 Latest from CENTCOM.mil</span></div>
  <div class="cb"><ul class="slist">{{CENTCOM_HTML}}</ul></div>
</div>

</main>
<div class="bar">
  <div>Generated by update.py · Data: OpenSky, Polymarket, Metaculus, CENTCOM RSS · Analysis: Claude Haiku 4.5</div>
  <div>IRAN WATCH v3.0 — UNCLASSIFIED // OPEN SOURCE</div>
</div>
</body></html>"""


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"IRAN WATCH — Daily Update")
    print(f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 60)

    # 0. Load yesterday's history for comparison
    history_path = os.path.join(os.path.dirname(__file__) or ".", "history.json")
    prev_callsigns = set()
    prev_markets = {}  # {question: {probability, volume}}
    try:
        with open(history_path, "r") as f:
            history = json.load(f)
            prev_callsigns = set(history.get("callsigns", []))
            for pm in history.get("markets", []):
                prev_markets[pm.get("question", "")] = pm
            print(f"[History] Loaded {len(prev_callsigns)} callsigns, {len(prev_markets)} markets from previous run")
    except FileNotFoundError:
        print("[History] No previous history — first run, all aircraft will show as NEW")
    except Exception as e:
        print(f"[History] Error loading history: {e}")

    # 1. Fetch all data sources
    opensky = fetch_opensky()
    polymarket = fetch_polymarket()
    metaculus = fetch_metaculus()
    centcom = fetch_centcom_rss()

    # 1b. Tag aircraft as new or returning
    current_callsigns = set()
    new_count = 0
    for ac in opensky.get("mil_aircraft", []):
        cs = ac["callsign"]
        current_callsigns.add(cs)
        if cs in prev_callsigns:
            ac["status"] = "returning"
        else:
            ac["status"] = "new"
            new_count += 1
    print(f"[History] {new_count} NEW aircraft, {len(current_callsigns) - new_count} returning")

    # 1c. Compute market deltas (probability changes and volume spikes)
    market_alerts = []
    PROB_SPIKE_THRESHOLD = 5     # Flag if probability moved ±5 points
    VOLUME_SPIKE_MULTIPLE = 3.0  # Flag if volume is 3x yesterday's
    for m in polymarket.get("markets", []):
        q = m["question"]
        prev = prev_markets.get(q)
        if prev:
            prob_delta = m["probability"] - prev.get("probability", m["probability"])
            m["prob_delta"] = prob_delta
            prev_vol = float(prev.get("volume", 0))
            curr_vol = float(m.get("volume", 0))
            vol_ratio = curr_vol / prev_vol if prev_vol > 0 else 0
            m["vol_ratio"] = round(vol_ratio, 1)
            # Flag unusual moves
            if abs(prob_delta) >= PROB_SPIKE_THRESHOLD:
                direction = "UP" if prob_delta > 0 else "DOWN"
                market_alerts.append(f"⚡ {q}: probability moved {direction} {abs(prob_delta)} pts (was {prev.get('probability', '?')}%, now {m['probability']}%)")
                print(f"[Markets] ALERT: {q} moved {prob_delta:+d} pts")
            if vol_ratio >= VOLUME_SPIKE_MULTIPLE and curr_vol > 10000:
                market_alerts.append(f"📈 {q}: trading volume surge — {vol_ratio:.1f}x yesterday's level")
                print(f"[Markets] ALERT: {q} volume spike {vol_ratio:.1f}x")
        else:
            m["prob_delta"] = None
            m["vol_ratio"] = None
    
    if market_alerts:
        print(f"[Markets] {len(market_alerts)} unusual market movements detected!")
    else:
        print("[Markets] No unusual movements detected")

    # Store market alerts on the polymarket dict so generate_html can use them
    polymarket["alerts"] = market_alerts

    # 1d. Save today's data for tomorrow's comparison
    try:
        market_snapshot = []
        for m in polymarket.get("markets", []):
            market_snapshot.append({
                "question": m["question"],
                "probability": m["probability"],
                "volume": m.get("volume", "0"),
            })
        with open(history_path, "w") as f:
            json.dump({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "callsigns": list(current_callsigns),
                "markets": market_snapshot,
            }, f)
        print(f"[History] Saved {len(current_callsigns)} callsigns, {len(market_snapshot)} markets to history.json")
    except Exception as e:
        print(f"[History] Error saving history: {e}")

    # 2. Generate AI analysis
    analysis = generate_analysis(opensky, polymarket, metaculus, centcom)

    # 3. Generate HTML
    html = generate_html(analysis, opensky, polymarket, metaculus, centcom)

    # 4. Write output
    output_path = os.path.join(os.path.dirname(__file__) or ".", "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n[Done] Written to {output_path}")
    print(f"  OpenSky: {opensky['status']} ({opensky.get('mil_count', 0)} mil aircraft)")
    print(f"  Polymarket: {polymarket['status']} ({len(polymarket.get('markets', []))} markets)")
    print(f"  Metaculus: {metaculus['status']} ({len(metaculus.get('questions', []))} questions)")
    print(f"  CENTCOM: {centcom['status']} ({len(centcom.get('releases', []))} releases)")
    print(f"  Claude: {'API key present' if ANTHROPIC_API_KEY else 'NO API KEY — fallback used'}")


if __name__ == "__main__":
    main()
