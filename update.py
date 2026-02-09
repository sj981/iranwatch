#!/usr/bin/env python3
"""
IRAN WATCH — OSINT Force Posture Monitor
Runs server-side (GitHub Actions) every 6 hours.
Fetches live data from free APIs, sends to Claude for analysis,
and generates an updated static HTML dashboard.

DATA SOURCES:
  - airplanes.live /mil endpoint (military aircraft, unfiltered ADS-B)
  - Polymarket Gamma API (prediction markets)
  - Metaculus API v2 (forecasting questions)
  - Kalshi API (regulated prediction markets)
  - USNI News Fleet Tracker (carrier strike group positions)
  - CENTCOM RSS (press releases)
  - Claude web search (diplomatic context, current events)

SETUP:
  1. GitHub repo with this file
  2. Secrets: ANTHROPIC_API_KEY (required), OPENSKY_CLIENT_ID/SECRET (optional fallback)
  3. GitHub Pages enabled on main branch
  4. Workflow runs every 6h: cron '0 5,11,17,23 * * *'

COST: ~$4-8/month (Claude Haiku 4.5, 4 calls/day + 4 web search calls/day)
"""

import glob
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ──────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
OPENSKY_CLIENT_ID = os.environ.get("OPENSKY_CLIENT_ID", "")
OPENSKY_CLIENT_SECRET = os.environ.get("OPENSKY_CLIENT_SECRET", "")

HISTORY_DIR = os.path.join(os.path.dirname(__file__) or ".", "history")
HISTORY_RETENTION_DAYS = 30

# Bounding box: Europe through South Asia
ME_BBOX = {"lamin": 10, "lamax": 55, "lomin": -10, "lomax": 70}

# Military callsign prefixes
MIL_PREFIXES = [
    # US airlift
    "RCH", "REACH", "PACK", "DUKE", "MOOSE", "FRED", "CARGO", "HERK",
    # US tankers
    "ETHYL", "JULIET", "PEARL", "STEEL", "SHELL", "TEAL", "NKAC",
    "PKSN", "BLUE", "CASA", "INDY", "GOLD", "IRON",
    # US ISR / AWACS / C2
    "HOMER", "TOPCT", "JAKE", "TITAN", "FORTE", "MAGIC", "SNTRY",
    "REDEYE", "RAIDR", "OLIVE", "MAZDA",
    # US fighters / bombers / strike
    "DOOM", "DEATH", "BATT", "MYTEE", "BONE", "VIPER", "EAGLE",
    "RAZOR", "HAWK", "STRIKE", "WRATH", "BOLT", "TABOR", "TREND", "RAGE",
    # US CSAR / VIP
    "KING", "PEDRO", "JOLLY", "DUSTOFF", "EVAC",
    "SAM", "VENUS", "EXEC", "SPAR",
    # US Navy
    "NAVY", "HAVOC", "CONDOR",
    # UK RAF
    "ASCOT", "RRR",
    # NATO / European
    "GAF", "FAF", "IAM",
    # Generic patterns
    "GOLD", "SHADOW", "TORCH", "TABOR",
]

CALLSIGN_AIRFRAMES = {
    # US Airlift
    "RCH":    ("C-17A Globemaster III", "Strategic airlift"),
    "REACH":  ("C-17A Globemaster III", "Strategic airlift"),
    "PACK":   ("C-5M Super Galaxy", "Heavy strategic airlift"),
    "DUKE":   ("C-17A Globemaster III", "Strategic airlift"),
    "MOOSE":  ("C-17A Globemaster III", "Strategic airlift"),
    "FRED":   ("C-130J Super Hercules", "Tactical airlift"),
    "CARGO":  ("C-17A / C-5M", "Strategic airlift"),
    "HERK":   ("C-130H/J Hercules", "Tactical airlift"),
    # US Tankers
    "ETHYL":  ("KC-135R Stratotanker", "Aerial refueling"),
    "JULIET": ("KC-135R Stratotanker", "Aerial refueling"),
    "PEARL":  ("KC-135R Stratotanker", "Aerial refueling"),
    "STEEL":  ("KC-135R Stratotanker", "Aerial refueling"),
    "SHELL":  ("KC-135R Stratotanker", "Aerial refueling"),
    "TEAL":   ("KC-10A Extender", "Aerial refueling"),
    "NKAC":   ("KC-135R Stratotanker", "Aerial refueling"),
    "PKSN":   ("KC-46A Pegasus", "Aerial refueling"),
    "BLUE":   ("KC-135R Stratotanker", "Aerial refueling"),
    "CASA":   ("KC-135R Stratotanker", "Aerial refueling"),
    "INDY":   ("KC-46A Pegasus", "Aerial refueling"),
    "GOLD":   ("KC-46A Pegasus", "Aerial refueling"),
    "IRON":   ("KC-135 Stratotanker", "Aerial refueling"),
    # ISR / AWACS / C2
    "HOMER":  ("RC-135V/W Rivet Joint", "SIGINT reconnaissance"),
    "TOPCT":  ("RC-135V/W Rivet Joint", "SIGINT reconnaissance"),
    "JAKE":   ("E-3G Sentry (AWACS)", "Airborne early warning"),
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
    "TABOR":  ("F-35A Lightning II", "Stealth fighter deployment"),
    "TREND":  ("F-15E Strike Eagle", "Strike fighter"),
    "RAGE":   ("F-16 Fighting Falcon", "Multirole fighter"),
    # CSAR / Medevac
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

# ICAO type code → human-readable name and role
_ICAO_TYPE_MAP = {
    "C17":("C-17A Globemaster III","Strategic airlift"),"C5M":("C-5M Super Galaxy","Heavy strategic airlift"),
    "C5":("C-5M Super Galaxy","Heavy strategic airlift"),"C130":("C-130 Hercules","Tactical airlift"),
    "C30J":("C-130J Super Hercules","Tactical airlift"),"A400":("A400M Atlas","Tactical/strategic airlift"),
    "A40M":("A400M Atlas","Tactical/strategic airlift"),
    "K35R":("KC-135R Stratotanker","Aerial refueling"),"K35E":("KC-135E Stratotanker","Aerial refueling"),
    "KC35":("KC-135 Stratotanker","Aerial refueling"),"K46":("KC-46A Pegasus","Aerial refueling"),
    "KC46":("KC-46A Pegasus","Aerial refueling"),"K10":("KC-10 Extender","Aerial refueling"),
    "KC10":("KC-10 Extender","Aerial refueling"),"MRTT":("A330 MRTT Voyager","Aerial refueling"),
    "GLEX":("RQ-4B Global Hawk / Bombardier","High-altitude ISR / Business"),
    "RQ4B":("RQ-4B Global Hawk","High-altitude ISR drone"),
    "E3CF":("E-3 Sentry AWACS","Airborne early warning"),"E3":("E-3 Sentry AWACS","Airborne early warning"),
    "E6":("E-6B Mercury","TACAMO / nuclear C3"),"E8":("E-8C JSTARS","Ground surveillance"),
    "P8":("P-8A Poseidon","Maritime patrol / ASW"),"P3":("P-3 Orion","Maritime patrol"),
    "RC35":("RC-135","SIGINT reconnaissance"),"E35L":("RC-135V/W Rivet Joint","SIGINT reconnaissance"),
    "B350":("MC-12W / King Air","ISR / light transport"),
    "B2":("B-2A Spirit","Stealth strategic bomber"),"B52":("B-52H Stratofortress","Strategic bomber"),
    "B1":("B-1B Lancer","Strategic bomber"),
    "F15":("F-15 Eagle/Strike Eagle","Air superiority / strike"),"F16":("F-16 Fighting Falcon","Multirole fighter"),
    "F18":("F/A-18 Hornet/Super Hornet","Carrier multirole fighter"),"F35":("F-35 Lightning II","Stealth multirole fighter"),
    "EUFI":("Eurofighter Typhoon","Multirole fighter"),"RFAL":("Rafale","Multirole fighter"),
    "H60":("UH-60 Black Hawk","Utility helicopter"),"H47":("CH-47 Chinook","Heavy lift helicopter"),
    "V22":("MV-22 Osprey","Tiltrotor transport"),
    "VC25":("VC-25A (Air Force One)","Presidential transport"),
}

def identify_airframe(callsign):
    if not callsign: return None
    cs = callsign.upper()
    for prefix, info in CALLSIGN_AIRFRAMES.items():
        if cs.startswith(prefix): return info
    return None

def _resolve_icao_type(type_code):
    if not type_code: return None
    tc = type_code.upper().replace("-", "")
    if tc in _ICAO_TYPE_MAP: return _ICAO_TYPE_MAP[tc]
    for key, val in _ICAO_TYPE_MAP.items():
        if tc.startswith(key) or key.startswith(tc): return val
    return None

def _country_from_hex(hex_code):
    try: h = int(hex_code, 16)
    except (ValueError, TypeError): return ""
    if 0xA00000 <= h <= 0xAFFFFF: return "United States"
    if 0x400000 <= h <= 0x43FFFF: return "United Kingdom"
    if 0x3C0000 <= h <= 0x3FFFFF: return "Germany"
    if 0x380000 <= h <= 0x3BFFFF: return "France"
    if 0x300000 <= h <= 0x33FFFF: return "Italy"
    if 0x340000 <= h <= 0x37FFFF: return "Spain"
    if 0x4C0000 <= h <= 0x4CFFFF: return "Turkey"
    if 0x738000 <= h <= 0x73FFFF: return "Israel"
    if 0x700000 <= h <= 0x70FFFF: return "Saudi Arabia"
    if 0x500000 <= h <= 0x507FFF: return "Australia"
    if 0xC00000 <= h <= 0xC3FFFF: return "Canada"
    return ""


# ──────────────────────────────────────────────────────────────
# OAUTH2 (for OpenSky fallback)
# ──────────────────────────────────────────────────────────────

def get_opensky_token():
    if not OPENSKY_CLIENT_ID or not OPENSKY_CLIENT_SECRET: return None
    try:
        resp = requests.post(
            "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token",
            data={"grant_type": "client_credentials",
                  "client_id": OPENSKY_CLIENT_ID,
                  "client_secret": OPENSKY_CLIENT_SECRET},
            timeout=15)
        resp.raise_for_status()
        return resp.json().get("access_token")
    except Exception as e:
        print(f"[OpenSky] OAuth2 error: {e}")
        return None


# ──────────────────────────────────────────────────────────────
# LOCATION RESOLVER
# ──────────────────────────────────────────────────────────────

_REFERENCE_POINTS = [
    (25.117, 51.315, "Al Udeid AB, Qatar"), (24.248, 54.547, "Al Dhafra AB, UAE"),
    (29.346, 47.521, "Ali Al Salem AB, Kuwait"), (32.356, 36.259, "Muwaffaq Salti AB, Jordan"),
    (24.062, 47.580, "Prince Sultan AB, Saudi Arabia"), (11.547, 43.155, "Camp Lemonnier, Djibouti"),
    (37.002, 35.426, "Incirlik AB, Turkey"), (34.590, 32.988, "RAF Akrotiri, Cyprus"),
    (26.236, 50.577, "NSA Bahrain"), (26.269, 50.162, "King Abdulaziz AB, KSA"),
    (21.450, 39.175, "King Faisal AB, Taif"), (23.943, 45.142, "Eskan Village, Riyadh"),
    (35.533, 44.362, "Kirkuk AB, Iraq"), (36.215, 37.225, "Aleppo, Syria"),
    (33.860, 35.490, "Beirut, Lebanon"), (32.010, 34.890, "Tel Aviv, Israel"),
    (35.175, 33.275, "Nicosia, Cyprus"), (38.575, 68.775, "Dushanbe, Tajikistan"),
    (34.350, 62.170, "Herat, Afghanistan"), (31.625, 65.850, "Kandahar, Afghanistan"),
    (33.950, 44.350, "Baghdad, Iraq"), (30.000, 47.800, "Basra, Iraq"),
    (36.340, 43.130, "Mosul, Iraq"), (35.690, 51.420, "Tehran, Iran"),
    (32.620, 51.680, "Isfahan, Iran"), (29.620, 52.530, "Shiraz, Iran"),
    (36.270, 59.610, "Mashhad, Iran"), (30.270, 56.960, "Kerman, Iran"),
    (37.530, 45.080, "Tabriz, Iran"), (27.170, 56.270, "Bandar Abbas, Iran"),
    (32.440, 53.100, "Natanz, Iran"), (33.730, 51.000, "Fordow, Iran"),
    # European staging bases
    (49.440, 7.600, "Ramstein AB, Germany"), (46.030, 11.900, "Aviano AB, Italy"),
    (36.800, -5.380, "Morón AB, Spain"), (36.645, -6.350, "Rota NAS, Spain"),
    (35.530, 24.150, "Souda Bay, Crete"), (37.400, 14.920, "NAS Sigonella, Sicily"),
    (52.360, 0.490, "RAF Lakenheath, UK"), (52.360, 0.490, "RAF Mildenhall, UK"),
    (55.510, -4.590, "Prestwick, Scotland"), (38.760, -27.090, "Lajes Field, Azores"),
]

_WATER_BODIES = [
    (24.0, 30.0, 45.0, 57.0, "the Persian Gulf"),
    (22.0, 27.0, 56.5, 62.0, "the Gulf of Oman"),
    (12.0, 22.0, 36.5, 45.0, "the Red Sea"),
    (12.5, 14.5, 42.5, 45.5, "the Bab el-Mandeb Strait"),
    (12.0, 30.0, 56.0, 70.0, "the Arabian Sea"),
    (30.0, 37.0, 26.0, 36.0, "the Eastern Mediterranean"),
    (35.0, 42.0, 26.0, 42.0, "the Black Sea"),
    (2.0, 12.0, 38.0, 52.0, "the Indian Ocean"),
    (36.0, 45.0, -6.0, 15.0, "the Western Mediterranean"),
    (46.0, 56.0, -10.0, 5.0, "the North Atlantic"),
]

_COUNTRY_BOXES = [
    (25.0,40.0,44.0,63.5,"Iran"),(29.0,37.5,38.5,48.5,"Iraq"),(15.5,32.5,34.5,56.0,"Saudi Arabia"),
    (22.5,26.5,51.0,56.5,"UAE"),(15.0,25.5,52.0,60.0,"Oman"),(28.5,30.5,46.5,48.5,"Kuwait"),
    (24.5,26.5,50.5,52.0,"Qatar"),(12.0,19.0,42.5,54.0,"Yemen"),(22.0,31.5,25.0,37.0,"Egypt"),
    (36.0,42.0,26.0,45.0,"Turkey"),(32.5,37.5,35.5,42.0,"Syria"),
    (29.0,33.5,35.0,39.5,"Jordan"),(29.5,33.5,34.0,35.9,"Israel"),
    (25.0,37.5,60.5,75.0,"Pakistan"),(29.5,38.5,60.5,71.5,"Afghanistan"),
    (-2.0,12.0,40.0,51.5,"Somalia"),(8.0,18.0,33.0,43.5,"Ethiopia"),
    (47.0,55.5,-10.0,2.0,"United Kingdom"),(46.0,55.5,5.5,15.5,"Germany"),
    (41.0,51.5,-5.5,10.0,"France"),(36.0,47.5,6.5,18.5,"Italy"),
    (36.0,44.0,-9.5,3.5,"Spain"),(35.0,42.0,19.5,30.0,"Greece"),
]

def _haversine_nm(lat1, lon1, lat2, lon2):
    R = 3440.065
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))

def describe_location(lat, lon):
    if not lat or not lon: return "unknown"
    nearest_ref, nearest_dist = None, 999999
    for rlat, rlon, rname in _REFERENCE_POINTS:
        d = _haversine_nm(lat, lon, rlat, rlon)
        if d < nearest_dist: nearest_dist, nearest_ref = d, rname
    if nearest_dist < 30:
        miles = round(nearest_dist * 1.151)
        return f"near {nearest_ref}" if miles < 10 else f"~{miles} mi from {nearest_ref}"
    for wlat_min, wlat_max, wlon_min, wlon_max, wname in _WATER_BODIES:
        if wlat_min <= lat <= wlat_max and wlon_min <= lon <= wlon_max:
            if nearest_dist < 60: return f"over {wname}, ~{round(nearest_dist*1.151)} mi from {nearest_ref}"
            return f"over {wname}"
    for clat_min, clat_max, clon_min, clon_max, cname in _COUNTRY_BOXES:
        if clat_min <= lat <= clat_max and clon_min <= lon <= clon_max:
            if nearest_dist < 100: return f"over {cname}, ~{round(nearest_dist*1.151)} mi from {nearest_ref}"
            return f"over {cname}"
    if nearest_ref: return f"~{round(nearest_dist*1.151)} mi from {nearest_ref}"
    return f"{lat:.1f}°N, {lon:.1f}°E"


# ──────────────────────────────────────────────────────────────
# HISTORY MANAGEMENT
# ──────────────────────────────────────────────────────────────

def load_history(days=7):
    """Load historical snapshots from the last N days."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    snapshots = []
    for fp in sorted(glob.glob(os.path.join(HISTORY_DIR, "*.json"))):
        try:
            fname = os.path.basename(fp)
            ts_str = fname.replace(".json", "")
            ts = datetime.fromisoformat(ts_str.replace("_", ":"))
            if ts >= cutoff:
                with open(fp) as f:
                    data = json.load(f)
                    data["_timestamp"] = ts.isoformat()
                    snapshots.append(data)
        except Exception:
            continue
    print(f"[History] Loaded {len(snapshots)} snapshots from last {days} days")
    return snapshots

def save_snapshot(data):
    """Save a timestamped snapshot."""
    os.makedirs(HISTORY_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H_%M_%S+00_00")
    fp = os.path.join(HISTORY_DIR, f"{ts}.json")
    with open(fp, "w") as f:
        json.dump(data, f)
    print(f"[History] Saved snapshot to {fp}")

def cleanup_old_history():
    """Remove snapshots older than retention period."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_RETENTION_DAYS)
    removed = 0
    for fp in glob.glob(os.path.join(HISTORY_DIR, "*.json")):
        try:
            fname = os.path.basename(fp)
            ts_str = fname.replace(".json", "")
            ts = datetime.fromisoformat(ts_str.replace("_", ":"))
            if ts < cutoff:
                os.remove(fp)
                removed += 1
        except Exception:
            continue
    if removed: print(f"[History] Cleaned up {removed} old snapshots")

def compute_trends(snapshots, current_markets):
    """Compute 24h and 7d probability trends for prediction markets."""
    if not snapshots: return {}
    now = datetime.now(timezone.utc)
    trends = {}
    for m in current_markets:
        q = m.get("question", "")
        h24_val, h7d_val = None, None
        for snap in reversed(snapshots):
            try:
                ts = datetime.fromisoformat(snap["_timestamp"])
                age_h = (now - ts).total_seconds() / 3600
                for pm in snap.get("markets", []):
                    if pm.get("question") == q:
                        if 20 <= age_h <= 30 and h24_val is None:
                            h24_val = pm.get("probability")
                        if 144 <= age_h <= 192 and h7d_val is None:
                            h7d_val = pm.get("probability")
            except Exception:
                continue
        trends[q] = {
            "delta_24h": m["probability"] - h24_val if h24_val is not None else None,
            "delta_7d": m["probability"] - h7d_val if h7d_val is not None else None,
            "prev_24h": h24_val,
            "prev_7d": h7d_val,
        }
    return trends

def compute_aircraft_baseline(snapshots):
    """Compute average military aircraft count from historical snapshots."""
    counts = [s.get("mil_count", 0) for s in snapshots if s.get("mil_count")]
    if not counts: return {"avg_7d": None, "max_7d": None, "samples": 0}
    return {
        "avg_7d": round(sum(counts) / len(counts), 1),
        "max_7d": max(counts),
        "samples": len(counts),
    }


# ──────────────────────────────────────────────────────────────
# DATA FETCHERS
# ──────────────────────────────────────────────────────────────

def fetch_aircraft():
    """Fetch military aircraft from airplanes.live /mil endpoint."""
    print("[airplanes.live] Fetching military aircraft...")
    try:
        resp = requests.get("https://api.airplanes.live/v2/mil", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        all_mil = data.get("ac", [])
        print(f"[airplanes.live] Global military aircraft: {len(all_mil)}")

        mil_aircraft = []
        for ac in all_mil:
            lat, lon = ac.get("lat"), ac.get("lon")
            if not lat or not lon: continue
            if ac.get("alt_baro") == "ground": continue
            if not (ME_BBOX["lamin"] <= lat <= ME_BBOX["lamax"] and
                    ME_BBOX["lomin"] <= lon <= ME_BBOX["lomax"]): continue

            callsign = (ac.get("flight") or "").strip().upper()
            hex_code = ac.get("hex", "")
            registration = ac.get("r", "")
            aircraft_type = ac.get("t", "")
            alt_ft = ac.get("alt_baro") if isinstance(ac.get("alt_baro"), (int, float)) else None
            gs_knots = ac.get("gs")

            airframe = None
            if aircraft_type: airframe = _resolve_icao_type(aircraft_type)
            if not airframe and callsign: airframe = identify_airframe(callsign)

            mil_aircraft.append({
                "callsign": callsign, "hex": hex_code, "registration": registration,
                "aircraft_type": aircraft_type, "origin": _country_from_hex(hex_code),
                "lat": round(lat, 2), "lon": round(lon, 2), "alt_ft": alt_ft,
                "gs_knots": round(gs_knots) if gs_knots else None,
                "location_desc": describe_location(round(lat, 2), round(lon, 2)),
                "airframe": airframe[0] if airframe else (aircraft_type or "Unknown type"),
                "role": airframe[1] if airframe else "Military",
            })

        mil_aircraft.sort(key=lambda a: (a["airframe"] == "Unknown type", -(a["alt_ft"] or 0)))
        print(f"[airplanes.live] Military in bounding box: {len(mil_aircraft)}")
        return {"status": "ok", "source": "airplanes.live", "total_aircraft": len(all_mil),
                "mil_count": len(mil_aircraft), "mil_aircraft": mil_aircraft[:50]}
    except Exception as e:
        print(f"[airplanes.live] Error: {e}, trying OpenSky fallback...")
        return _fetch_opensky_fallback()

def _fetch_opensky_fallback():
    print("[OpenSky Fallback] Fetching...")
    try:
        headers = {}
        token = get_opensky_token()
        if token: headers["Authorization"] = f"Bearer {token}"
        resp = requests.get("https://opensky-network.org/api/states/all",
            params={"lamin": ME_BBOX["lamin"], "lamax": ME_BBOX["lamax"],
                    "lomin": ME_BBOX["lomin"], "lomax": ME_BBOX["lomax"]},
            headers=headers, timeout=30)
        resp.raise_for_status()
        all_ac = resp.json().get("states", []) or []
        mil = []
        for ac in all_ac:
            cs = (ac[1] or "").strip().upper()
            if ac[8] or not ac[6] or not ac[5]: continue
            if not any(cs.startswith(p) for p in MIL_PREFIXES): continue
            af = identify_airframe(cs)
            mil.append({"callsign": cs, "hex": ac[0], "registration": "", "aircraft_type": "",
                "origin": ac[2] or "", "lat": round(ac[6],2), "lon": round(ac[5],2),
                "alt_ft": round(ac[7]*3.281) if ac[7] else None,
                "location_desc": describe_location(round(ac[6],2), round(ac[5],2)),
                "airframe": af[0] if af else "Unknown type", "role": af[1] if af else "Military"})
        return {"status": "ok (OpenSky fallback)", "source": "opensky",
                "total_aircraft": len(all_ac), "mil_count": len(mil), "mil_aircraft": mil[:30]}
    except Exception as e:
        return {"status": "error", "error": str(e), "mil_count": 0, "mil_aircraft": []}


def fetch_polymarket():
    """Fetch Iran-related prediction markets, deduplicated and categorised."""
    print("[Polymarket] Fetching Iran markets...")
    url = "https://gamma-api.polymarket.com/events"
    try:
        markets, seen = [], set()
        for tag in ["iran", "middle-east", "geopolitics", "us-foreign-policy"]:
            try:
                resp = requests.get(url, params={"tag": tag, "active": "true", "closed": "false", "limit": 50}, timeout=15)
                resp.raise_for_status()
                for ev in resp.json():
                    for m in ev.get("markets", []):
                        q = (m.get("question") or "").lower()
                        if any(x in q for x in ["world cup","soccer","football","olympics","gdp","inflation","bitcoin","crypto","stock","etf"]): continue
                        if not any(kw in q for kw in ["iran","tehran","khamenei","irgc","fordow","natanz","strike","centcom","persian gulf","strait of hormuz","nuclear","enrichment","regime change","us attack iran","us strike iran","bomb iran"]): continue
                        prices = json.loads(m.get("outcomePrices", "[]"))
                        yes_price = round(float(prices[0]) * 100) if prices else None
                        if yes_price is None: continue
                        # Dedup
                        norm = q.replace("?","").strip()
                        for frag in ["by january","by february","by march","by april","by may","by june","by july","by august","by september","by october","by november","by december","in 2025","in 2026","before 2026","before 2027","by 2026","by 2027"]:
                            norm = norm.replace(frag, "")
                        norm = norm.strip()
                        if norm in seen: continue
                        seen.add(norm)
                        cat = "other"
                        if any(x in q for x in ["us strike","us attack","america strike","united states strike"]): cat = "us_strike"
                        elif any(x in q for x in ["israel strike","israel attack","idf strike","israeli strike"]): cat = "israel_strike"
                        elif any(x in q for x in ["ceasefire","peace","deal","agreement","negotiat"]): cat = "ceasefire"
                        elif any(x in q for x in ["nuclear","enrichment","weapon","warhead"]): cat = "nuclear"
                        elif any(x in q for x in ["war","conflict","military","strike","attack"]): cat = "conflict"
                        markets.append({"question": m.get("question",""), "probability": yes_price,
                            "volume": m.get("volume","0"), "url": f"https://polymarket.com/event/{ev.get('slug','')}",
                            "category": cat, "source": "polymarket"})
            except Exception: pass
        cat_order = {"us_strike":0,"israel_strike":1,"conflict":2,"nuclear":3,"ceasefire":4,"other":5}
        markets.sort(key=lambda x: (cat_order.get(x.get("category","other"),5), -float(x.get("volume",0))))
        print(f"[Polymarket] Found {len(markets)} Iran markets (deduped)")
        return {"status": "ok", "markets": markets[:10]}
    except Exception as e:
        print(f"[Polymarket] Error: {e}")
        return {"status": "error", "error": str(e), "markets": []}


def fetch_kalshi():
    """Fetch Iran-related markets from Kalshi public API."""
    print("[Kalshi] Fetching markets...")
    try:
        resp = requests.get("https://api.elections.kalshi.com/trade-api/v2/events",
            params={"status": "open", "series_ticker": "IRAN"}, timeout=15,
            headers={"Accept": "application/json"})
        if resp.status_code != 200:
            # Try searching by keyword
            resp = requests.get("https://api.elections.kalshi.com/trade-api/v2/events",
                params={"status": "open"}, timeout=15,
                headers={"Accept": "application/json"})
        markets = []
        if resp.status_code == 200:
            data = resp.json()
            for ev in data.get("events", []):
                title = (ev.get("title") or "").lower()
                if any(kw in title for kw in ["iran", "strike iran", "attack iran"]):
                    for m in ev.get("markets", []):
                        yes_price = m.get("yes_bid")
                        if yes_price is not None:
                            markets.append({
                                "question": m.get("title", ev.get("title", "")),
                                "probability": round(yes_price * 100) if yes_price < 1 else yes_price,
                                "volume": str(m.get("volume", 0)),
                                "url": f"https://kalshi.com/events/{ev.get('ticker', '')}",
                                "category": "us_strike", "source": "kalshi",
                            })
        print(f"[Kalshi] Found {len(markets)} Iran markets")
        return {"status": "ok", "markets": markets[:5]}
    except Exception as e:
        print(f"[Kalshi] Error: {e}")
        return {"status": "error", "error": str(e), "markets": []}


def fetch_metaculus():
    """Fetch Iran questions from Metaculus — known IDs + search."""
    print("[Metaculus] Fetching Iran questions...")
    KNOWN_IDS = [41594, 31498, 31327, 32764]
    questions = []
    headers = {"Accept": "application/json", "User-Agent": "IranWatch/2.0"}
    try:
        for qid in KNOWN_IDS:
            try:
                resp = requests.get(f"https://www.metaculus.com/api2/questions/{qid}/", timeout=10, headers=headers)
                if resp.status_code == 200:
                    q = resp.json()
                    cp = q.get("community_prediction", {})
                    full = cp.get("full", {}) if isinstance(cp, dict) else {}
                    median = full.get("q2") if isinstance(full, dict) else None
                    if median is not None and q.get("status") in ("open", "upcoming", ""):
                        questions.append({"question": q.get("title",""), "probability": round(median*100),
                            "forecasters": q.get("number_of_predictions", q.get("nr_forecasters",0)),
                            "url": f"https://www.metaculus.com/questions/{qid}/", "id": qid, "source": "metaculus"})
            except Exception: pass
        for term in ["iran strike", "iran nuclear", "iran attack"]:
            try:
                resp = requests.get("https://www.metaculus.com/api2/questions/",
                    params={"search": term, "status": "open", "limit": 10, "type": "binary", "order_by": "-activity"},
                    timeout=15, headers=headers)
                if resp.status_code == 200:
                    for q in resp.json().get("results", []):
                        title = (q.get("title") or "").lower()
                        qid = q.get("id")
                        if any(x.get("id") == qid for x in questions): continue
                        if any(kw in title for kw in ["iran","tehran","irgc","natanz","fordow"]):
                            cp = q.get("community_prediction", {})
                            full = cp.get("full", {}) if isinstance(cp, dict) else {}
                            median = full.get("q2") if isinstance(full, dict) else None
                            if median is not None:
                                questions.append({"question": q.get("title",""), "probability": round(median*100),
                                    "forecasters": q.get("number_of_predictions", q.get("nr_forecasters",0)),
                                    "url": f"https://www.metaculus.com/questions/{qid}/", "id": qid, "source": "metaculus"})
            except Exception: pass
        print(f"[Metaculus] Found {len(questions)} Iran questions")
        return {"status": "ok", "questions": questions[:10]}
    except Exception as e:
        print(f"[Metaculus] Error: {e}")
        return {"status": "error", "error": str(e), "questions": []}


def fetch_centcom_rss():
    """Fetch latest CENTCOM press releases."""
    print("[CENTCOM] Fetching RSS feed...")
    try:
        headers = {"User-Agent": "IranWatch/2.0 (OSINT Monitor)"}
        resp = requests.get("https://www.centcom.mil/RSS/", headers=headers, timeout=15)
        if resp.status_code == 403:
            resp = requests.get("https://www.centcom.mil/MEDIA/PRESS-RELEASES/", headers=headers, timeout=15)
        resp.raise_for_status()
        titles = re.findall(r"<title><!\[CDATA\[(.*?)\]\]></title>", resp.text)
        if not titles: titles = re.findall(r"<title>(.*?)</title>", resp.text)
        releases = [t.strip() for t in titles[:15] if t.strip() and "CENTCOM" not in t[:10]]
        print(f"[CENTCOM] Found {len(releases)} recent releases")
        return {"status": "ok", "releases": releases}
    except Exception as e:
        print(f"[CENTCOM] Error: {e}")
        return {"status": "error", "error": str(e), "releases": []}


def fetch_naval():
    """Fetch carrier strike group positions from USNI News Fleet Tracker."""
    print("[Naval] Fetching USNI Fleet Tracker...")
    try:
        resp = requests.get("https://news.usni.org/category/fleet-tracker",
            headers={"User-Agent": "IranWatch/2.0"}, timeout=15)
        if resp.status_code != 200:
            print(f"[Naval] USNI returned {resp.status_code}")
            return {"status": "error", "error": f"HTTP {resp.status_code}", "carriers": []}

        # Extract the most recent fleet tracker article URL
        article_urls = re.findall(r'href="(https://news\.usni\.org/\d{4}/\d{2}/\d{2}/usni-news-fleet-and-marine-tracker[^"]*)"', resp.text)
        if not article_urls:
            article_urls = re.findall(r'href="(https://news\.usni\.org/\d{4}/\d{2}/\d{2}/[^"]*fleet[^"]*tracker[^"]*)"', resp.text, re.I)

        if not article_urls:
            print("[Naval] No fleet tracker article found")
            return {"status": "partial", "error": "No recent article found", "carriers": [],
                    "raw_text": "USNI Fleet Tracker page accessible but no recent article URL extracted."}

        # Fetch the most recent article
        article_url = article_urls[0]
        print(f"[Naval] Fetching article: {article_url}")
        art_resp = requests.get(article_url, headers={"User-Agent": "IranWatch/2.0"}, timeout=15)

        if art_resp.status_code != 200:
            return {"status": "partial", "error": f"Article HTTP {art_resp.status_code}", "carriers": []}

        # Extract text content (strip HTML)
        text = re.sub(r'<[^>]+>', ' ', art_resp.text)
        text = re.sub(r'\s+', ' ', text)

        # Look for carrier group mentions
        carriers = []
        carrier_patterns = [
            r'USS\s+([\w\s]+?)\s*\(CVN[- ]?\d+\)',
            r'(Nimitz|Eisenhower|Lincoln|Truman|Reagan|Stennis|Vinson|Roosevelt|Washington|Bush|Ford|Enterprise)\s+(?:Carrier\s+)?Strike\s+Group',
            r'CSG[- ]?\d+',
        ]
        for pat in carrier_patterns:
            for match in re.finditer(pat, text, re.I):
                name = match.group(0).strip()
                if name not in [c["name"] for c in carriers]:
                    # Try to find location context (100 chars around the match)
                    start = max(0, match.start() - 200)
                    end = min(len(text), match.end() + 200)
                    context = text[start:end]
                    carriers.append({"name": name, "context": context[:300]})

        # Extract the date from the article
        date_match = re.search(r'(\w+ \d{1,2}, \d{4})', text[:500])
        article_date = date_match.group(1) if date_match else "Unknown date"

        print(f"[Naval] Found {len(carriers)} carrier references, article date: {article_date}")
        return {
            "status": "ok",
            "article_url": article_url,
            "article_date": article_date,
            "carriers": carriers[:8],
            "raw_text": text[:3000],  # First 3000 chars for Claude to analyze
        }
    except Exception as e:
        print(f"[Naval] Error: {e}")
        return {"status": "error", "error": str(e), "carriers": []}


def fetch_diplomatic_context():
    """Use Claude with web search to get current diplomatic context."""
    print("[Diplomatic] Fetching current context via Claude web search...")
    if not ANTHROPIC_API_KEY:
        return {"status": "no_api_key", "context": ""}
    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "content-type": "application/json",
                     "anthropic-version": "2023-06-01"},
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": f"""Today is {datetime.now(timezone.utc).strftime('%d %B %Y')}. Search for the latest news about:
1. Any current or upcoming US diplomatic travel to the Middle East, Caucasus, or Gulf region (VP, SecState, SecDef visits)
2. Any Iran-related negotiations, nuclear talks, or diplomatic signals in the last 48 hours
3. Any US military exercises or deployments announced in the CENTCOM AOR in the last 48 hours
4. Any significant Iran military actions or provocations in the last 48 hours

Provide a concise 3-5 bullet point summary of the most important diplomatic and military context. Focus on facts that would help explain or contextualise military aircraft movements in the region. Be specific about names, dates, and locations."""}],
            }, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        text_parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        context = "\n".join(text_parts).strip()
        print(f"[Diplomatic] Got {len(context)} chars of context")
        return {"status": "ok", "context": context}
    except Exception as e:
        print(f"[Diplomatic] Error: {e}")
        return {"status": "error", "error": str(e), "context": ""}


# ──────────────────────────────────────────────────────────────
# CLAUDE ANALYSIS
# ──────────────────────────────────────────────────────────────

def generate_analysis(aircraft, polymarket, metaculus, centcom, naval, diplomatic, kalshi, ac_baseline, market_trends):
    """Send all collected data to Claude for IC-style analysis."""
    print("[Claude] Generating analysis...")
    if not ANTHROPIC_API_KEY:
        return generate_fallback_analysis(aircraft, polymarket, metaculus, centcom)

    now_utc = datetime.now(timezone.utc)
    date_str = now_utc.strftime("%A, %d %B %Y")

    # Merge all prediction markets for the data summary
    all_markets = polymarket.get("markets", []) + kalshi.get("markets", [])
    for m in all_markets:
        q = m.get("question", "")
        if q in market_trends:
            m["delta_24h"] = market_trends[q].get("delta_24h")
            m["delta_7d"] = market_trends[q].get("delta_7d")

    data_summary = f"""
## LIVE DATA COLLECTED AT {now_utc.strftime('%Y-%m-%d %H:%M UTC')}

### Aircraft Tracking (airplanes.live, bounding box lat 10-55°N, lon 10°W-70°E)
Source: {aircraft.get('source', 'airplanes.live')}
Status: {aircraft['status']}
Military aircraft in region: {aircraft.get('mil_count', 0)}
Global military broadcasting: {aircraft.get('total_aircraft', 'N/A')}
7-day average aircraft count: {ac_baseline.get('avg_7d', 'N/A')} (max: {ac_baseline.get('max_7d', 'N/A')}, from {ac_baseline.get('samples', 0)} samples)
Aircraft details: {json.dumps(aircraft.get('mil_aircraft', [])[:20], indent=2)}

### Prediction Markets (Polymarket + Kalshi + Metaculus)
Polymarket status: {polymarket['status']} ({len(polymarket.get('markets', []))} markets)
Kalshi status: {kalshi['status']} ({len(kalshi.get('markets', []))} markets)
Metaculus status: {metaculus['status']} ({len(metaculus.get('questions', []))} questions)
Market data (with 24h/7d changes where available): {json.dumps(all_markets + metaculus.get('questions', []), indent=2)}

### Naval Forces (USNI Fleet Tracker)
Status: {naval['status']}
Article date: {naval.get('article_date', 'N/A')}
Carrier references: {json.dumps(naval.get('carriers', []), indent=2)}
Fleet tracker summary: {naval.get('raw_text', 'N/A')[:2000]}

### CENTCOM RSS
Status: {centcom['status']}
Recent releases: {json.dumps(centcom.get('releases', []), indent=2)}

### Current Diplomatic Context (from web search)
{diplomatic.get('context', 'Not available')}
"""

    system_prompt = """You are an intelligence analyst producing a 6-hourly open-source intelligence (OSINT) briefing on US military posture toward Iran. Write in IC (Intelligence Community) style with confidence levels.

CRITICAL: CONTEXTUALISE MILITARY MOVEMENTS
Do NOT treat all military aircraft movements as Iran-related. Apply Occam's Razor:
- Tanker movements from European bases may support routine deployments, exercises, or non-Iran missions
- VIP transports near the Caucasus/Turkey may relate to diplomatic travel
- Fighter deployments from CONUS may be routine rotational replacements
- C-17 airlift near Gulf bases is continuous and not inherently escalatory
- Use the diplomatic context provided to explain movements where possible
Only flag movements as Iran-significant when matching I&W patterns: unusual tanker surges above baseline, bomber repositioning, ISR orbit changes, SEAD/DEAD package assembly, carrier strike group movements.

AIRCRAFT DATA FIELDS:
Each aircraft includes: airframe, role, location_desc, origin, registration, hex code.
Aircraft data also includes 7-day average count — compare current count to baseline.

PREDICTION MARKETS:
Markets include 24h and 7d probability changes where available. You MUST clearly state whether probabilities have risen or fallen and by how much. Distinguish between US-strike and Israel-strike markets.

NAVAL FORCES:
The USNI Fleet Tracker data shows carrier strike group positions. Note any CSGs in the Gulf, Arabian Sea, or Mediterranean as these are directly relevant.

Write aircraft descriptions in plain English (no callsigns in prose). Group similar aircraft.

Output a JSON object with exactly these keys:
{
  "threat_level": "HIGH" or "CRITICAL" or "ELEVATED" or "ROUTINE",
  "threat_summary": "2-3 sentences. Mention aircraft types, naval positions, and market direction. Note benign explanations where applicable.",
  "key_judgment": "IC-style key judgment paragraph with confidence level. Reference both military posture and market consensus.",
  "overnight_summary": "2-3 sentences on what changed since last update. Plain English aircraft descriptions. Note diplomatic context.",
  "activity_groups": [
    {"title": "Group Title", "icon": "critical|notable|routine", "body": "Summary with source tags."}
  ],
  "prediction_markets_summary": "2-3 sentences. State clearly whether probabilities have RISEN or FALLEN and by how much. Note any volume spikes.",
  "naval_summary": "1-2 sentences on carrier strike group positions and any notable naval movements.",
  "diplomatic_summary": "2-3 bullet points on diplomatic situation including VIP travel and negotiations.",
  "iw_updates": "Any updates to I&W indicators."
}

Date: """ + date_str + """

Aircraft data from airplanes.live (unfiltered ADS-B, military-tagged). Many aircraft fly without transponders — partial picture. Includes type codes, registrations, hex IDs."""

    try:
        resp = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "content-type": "application/json", "anthropic-version": "2023-06-01"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 4096, "system": system_prompt,
                  "messages": [{"role": "user", "content": f"Generate the briefing.\n\n{data_summary}"}]},
            timeout=90)
        resp.raise_for_status()
        content = resp.json().get("content", [{}])[0].get("text", "")
        # Parse JSON from response
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            analysis = json.loads(json_match.group())
            print("[Claude] Analysis generated successfully")
            return analysis
    except Exception as e:
        print(f"[Claude] Error: {e}")
    return generate_fallback_analysis(aircraft, polymarket, metaculus, centcom)


def generate_fallback_analysis(aircraft, polymarket, metaculus, centcom):
    """Offline fallback when Claude API is unavailable."""
    mil_list = aircraft.get("mil_aircraft", [])
    types = {}
    for a in mil_list:
        r = a.get("role", "Military")
        types[r] = types.get(r, 0) + 1
    ac_str = ", ".join(f"{v} {k}" for k, v in sorted(types.items(), key=lambda x: -x[1])[:4])
    pm_markets = polymarket.get("markets", [])
    pm_summary = f"Tracking {len(pm_markets)} prediction markets." if pm_markets else "Prediction market data unavailable."

    return {
        "threat_level": "ELEVATED",
        "threat_summary": f"Monitoring {aircraft.get('mil_count','unknown')} military aircraft in region. {ac_str}. {pm_summary}",
        "key_judgment": "Assessment generated without AI analysis — API unavailable. Data should be interpreted with caution.",
        "overnight_summary": f"Detected {aircraft.get('mil_count',0)} military aircraft broadcasting ADS-B. CENTCOM published {len(centcom.get('releases',[]))} releases.",
        "activity_groups": [{"title": "Automated Data Collection", "icon": "routine", "body": f"Aircraft: {aircraft.get('mil_count',0)} military. Markets: {len(pm_markets)} tracked. CENTCOM: {len(centcom.get('releases',[]))} releases. [Automated — no AI analysis]"}],
        "prediction_markets_summary": pm_summary,
        "naval_summary": "Naval data requires AI analysis for interpretation.",
        "diplomatic_summary": "Diplomatic context requires AI analysis.",
        "iw_updates": "No AI-generated I&W updates available.",
    }


# ──────────────────────────────────────────────────────────────
# HTML GENERATION
# ──────────────────────────────────────────────────────────────

def generate_html(analysis, aircraft, polymarket, metaculus, centcom, naval, kalshi, market_trends, ac_baseline, snapshots):
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%d %B %Y")
    time_str = now.strftime("%H:%M UTC")
    threat_level = analysis.get("threat_level", "ELEVATED")
    tl = threat_level.lower()

    def s(val):
        if isinstance(val, list): return "<br>".join(str(i) for i in val)
        return str(val) if val else ""

    # Build activity groups HTML
    groups_html = ""
    for g in analysis.get("activity_groups", []):
        icon = g.get("icon", "routine")
        groups_html += f'<div class="ag"><div class="ag-dot {icon}"></div><div><div class="ag-t">{g["title"]}</div><div class="ag-b">{g["body"]}</div></div></div>'

    # Build all markets (Polymarket + Kalshi + Metaculus)
    all_pm = polymarket.get("markets", []) + kalshi.get("markets", [])
    markets_html = ""
    cat_badges = {
        "us_strike": ("US STRIKE", "#ef4444"), "israel_strike": ("ISRAEL STRIKE", "#f59e0b"),
        "conflict": ("CONFLICT", "#f59e0b"), "nuclear": ("NUCLEAR", "#ef4444"),
        "ceasefire": ("DIPLOMATIC", "#06b6d4"),
    }
    for m in all_pm:
        prob = m["probability"]
        col = "#ef4444" if prob >= 60 else "#f59e0b" if prob >= 40 else "#94a3b8"
        vol = float(m.get("volume", 0))
        vol_str = f"${vol/1e6:.1f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K" if vol >= 1e3 else f"${vol:.0f}"
        cat = m.get("category", "other")
        badge_label, badge_col = cat_badges.get(cat, ("", "#94a3b8"))
        badge_html = f'<span class="cat-badge" style="--c:{badge_col}">{badge_label}</span>' if badge_label else ""
        src_label = m.get("source", "polymarket").capitalize()
        # Delta
        q = m.get("question", "")
        trend = market_trends.get(q, {})
        delta_24h = trend.get("delta_24h") or m.get("delta_24h")
        delta_7d = trend.get("delta_7d") or m.get("delta_7d")
        delta_html = ""
        if delta_24h is not None and delta_24h != 0:
            arrow = "↑" if delta_24h > 0 else "↓"
            dcol = "#ef4444" if delta_24h > 0 else "#22c55e"
            weight = "700" if abs(delta_24h) >= 5 else "400"
            delta_html += f'<span class="delta" style="color:{dcol};font-weight:{weight}">{arrow}{abs(delta_24h)}pts 24h</span>'
        if delta_7d is not None and delta_7d != 0:
            arrow = "↑" if delta_7d > 0 else "↓"
            dcol = "#ef4444" if delta_7d > 0 else "#22c55e"
            delta_html += f'<span class="delta" style="color:{dcol}">{arrow}{abs(delta_7d)}pts 7d</span>'

        url = m.get("url", "#")
        markets_html += f'''<div class="mkt">
          <div class="mkt-info">{badge_html}<a href="{url}" target="_blank" class="mkt-q">{m["question"]}</a><div class="mkt-meta">{src_label} · Vol: {vol_str}</div></div>
          <div class="mkt-right"><div class="mkt-prob" style="color:{col}">{prob}%</div>{delta_html}</div></div>'''

    # Metaculus
    for q in metaculus.get("questions", []):
        prob = q["probability"]
        col = "#ef4444" if prob >= 60 else "#f59e0b" if prob >= 40 else "#94a3b8"
        url = q.get("url", "#")
        markets_html += f'''<div class="mkt">
          <div class="mkt-info"><a href="{url}" target="_blank" class="mkt-q">{q["question"]}</a><div class="mkt-meta">Metaculus · {q.get("forecasters","?")} forecasters</div></div>
          <div class="mkt-right"><div class="mkt-prob" style="color:{col}">{prob}%</div></div></div>'''

    if not markets_html:
        markets_html = '<div class="mkt"><div class="mkt-info mkt-q" style="color:#64748b">No prediction market data available.</div></div>'

    # Aircraft table
    mil_html = ""
    for a in aircraft.get("mil_aircraft", [])[:25]:
        label = a.get("callsign") or a.get("registration") or a.get("hex", "?")
        status_cls = "new" if a.get("status") == "new" else ""
        mil_html += f'<tr class="{status_cls}"><td class="ac-label">{label}</td><td>{a["airframe"]}</td><td class="ac-role">{a["role"]}</td><td>{a["location_desc"]}</td><td class="ac-alt">{a["alt_ft"]:,} ft</td></tr>' if a.get("alt_ft") else f'<tr class="{status_cls}"><td class="ac-label">{label}</td><td>{a["airframe"]}</td><td class="ac-role">{a["role"]}</td><td>{a["location_desc"]}</td><td class="ac-alt">—</td></tr>'

    # CENTCOM
    centcom_html = ""
    for r in centcom.get("releases", [])[:8]:
        centcom_html += f'<div class="centcom-item">{r}</div>'

    # Naval
    naval_html = s(analysis.get("naval_summary", ""))
    naval_carriers = naval.get("carriers", [])
    naval_date = naval.get("article_date", "")

    # Aircraft baseline comparison
    baseline = ac_baseline
    current_count = aircraft.get("mil_count", 0)
    avg = baseline.get("avg_7d")
    baseline_html = ""
    if avg and avg > 0:
        ratio = current_count / avg
        if ratio > 1.5:
            baseline_html = f'<span class="baseline-alert high">{current_count} aircraft — {ratio:.1f}× above 7-day average ({avg})</span>'
        elif ratio > 1.2:
            baseline_html = f'<span class="baseline-alert moderate">{current_count} aircraft — {ratio:.1f}× above 7-day average ({avg})</span>'
        else:
            baseline_html = f'<span class="baseline-normal">{current_count} aircraft (7-day avg: {avg})</span>'
    else:
        baseline_html = f'<span class="baseline-normal">{current_count} military aircraft detected</span>'

    # API status
    feeds = [
        ("airplanes.live", aircraft["status"]),
        ("Polymarket", polymarket["status"]),
        ("Metaculus", metaculus["status"]),
        ("Kalshi", kalshi["status"]),
        ("USNI Naval", naval["status"]),
        ("CENTCOM", centcom["status"]),
    ]
    feeds_html = ""
    for name, st in feeds:
        dot = "ok" if st == "ok" else "err"
        feeds_html += f'<span class="feed"><span class="fd {dot}"></span>{name}</span>'

    # Inject into template
    html = HTML_TEMPLATE
    replacements = {
        "{{DATE}}": date_str, "{{TIME}}": time_str,
        "{{THREAT_LEVEL}}": threat_level, "{{TL}}": tl,
        "{{THREAT_SUMMARY}}": s(analysis.get("threat_summary", "")),
        "{{KEY_JUDGMENT}}": s(analysis.get("key_judgment", "")),
        "{{OVERNIGHT}}": s(analysis.get("overnight_summary", "")),
        "{{GROUPS}}": groups_html,
        "{{MARKETS_HTML}}": markets_html,
        "{{MARKETS_SUMMARY}}": s(analysis.get("prediction_markets_summary", "")),
        "{{NAVAL_SUMMARY}}": naval_html,
        "{{NAVAL_DATE}}": naval_date,
        "{{DIPLOMATIC}}": s(analysis.get("diplomatic_summary", "")),
        "{{IW}}": s(analysis.get("iw_updates", "")),
        "{{MIL_HTML}}": mil_html,
        "{{MIL_COUNT}}": str(aircraft.get("mil_count", 0)),
        "{{BASELINE}}": baseline_html,
        "{{CENTCOM_HTML}}": centcom_html,
        "{{FEEDS}}": feeds_html,
        "{{AIRCRAFT_JSON}}": json.dumps([{
            "callsign": a.get("callsign",""), "hex": a.get("hex",""), "registration": a.get("registration",""),
            "lat": a.get("lat"), "lon": a.get("lon"), "alt_ft": a.get("alt_ft"),
            "origin": a.get("origin",""), "status": a.get("status","new"),
            "location_desc": a.get("location_desc",""), "airframe": a.get("airframe",""), "role": a.get("role",""),
        } for a in aircraft.get("mil_aircraft", [])]),
    }
    for k, v in replacements.items():
        html = html.replace(k, v)
    return html


# ──────────────────────────────────────────────────────────────
# HTML TEMPLATE
# ──────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>IRAN WATCH</title>
<link href="https://fonts.googleapis.com/css2?family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;0,9..40,600;0,9..40,700;1,9..40,400&family=JetBrains+Mono:wght@400;500;600;700&family=Newsreader:ital,opsz,wght@0,6..72,400;0,6..72,500;1,6..72,400&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#080a0f;--bg2:#0c0f16;--bg3:#111520;--bg4:#171c28;--border:#1a2035;--border2:#242d45;--text:#e2e8f0;--text2:#94a3b8;--text3:#64748b;--text4:#475569;--red:#ef4444;--amber:#f59e0b;--green:#22c55e;--blue:#3b82f6;--cyan:#06b6d4;--red-bg:rgba(239,68,68,.08);--amber-bg:rgba(245,158,11,.08);--green-bg:rgba(34,197,94,.08);--blue-bg:rgba(59,130,246,.08)}
body{background:var(--bg);color:var(--text);font-family:'DM Sans',system-ui,sans-serif;font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased}
a{color:var(--cyan);text-decoration:none}a:hover{text-decoration:underline}
.hdr{border-bottom:1px solid var(--border);background:var(--bg2);position:sticky;top:0;z-index:100;backdrop-filter:blur(16px)}
.hdr-inner{max-width:1080px;margin:0 auto;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:12px}
.pulse{width:10px;height:10px;border-radius:50%;background:var(--red);box-shadow:0 0 12px var(--red);animation:p 2s ease-in-out infinite}
@keyframes p{0%,100%{opacity:1;box-shadow:0 0 12px var(--red)}50%{opacity:.6;box-shadow:0 0 24px var(--red)}}
.brand-text{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:16px;letter-spacing:5px;text-transform:uppercase}
.hdr-right{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text3);text-align:right;line-height:1.7}
.hdr-right strong{color:var(--text2);font-weight:600}
.main{max-width:1080px;margin:0 auto;padding:24px 24px 80px}
.threat{border-radius:8px;padding:24px;margin-bottom:32px;display:flex;gap:20px;align-items:flex-start}
.threat.high{background:linear-gradient(135deg,rgba(245,158,11,.1),rgba(245,158,11,.02));border:1px solid rgba(245,158,11,.2)}
.threat.critical{background:linear-gradient(135deg,rgba(239,68,68,.12),rgba(239,68,68,.02));border:1px solid rgba(239,68,68,.25)}
.threat.elevated{background:linear-gradient(135deg,rgba(59,130,246,.1),rgba(59,130,246,.02));border:1px solid rgba(59,130,246,.2)}
.threat.routine{background:var(--bg3);border:1px solid var(--border)}
.tl-badge{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12px;letter-spacing:2px;padding:8px 16px;border-radius:6px;white-space:nowrap;flex-shrink:0}
.tl-badge.high{background:var(--amber);color:#000}.tl-badge.critical{background:var(--red);color:#fff}
.tl-badge.elevated{background:var(--blue);color:#fff}.tl-badge.routine{background:var(--green);color:#000}
.threat-body{flex:1;min-width:0}
.threat-body p{font-size:15px;line-height:1.65}
.threat-body .scale{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text4);margin-top:12px;letter-spacing:.5px;line-height:2}
.threat-body .scale span{padding:2px 8px;border-radius:3px;margin-right:2px}
.sec{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;letter-spacing:3px;text-transform:uppercase;color:var(--text4);margin:36px 0 16px;padding-bottom:10px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between}
.tag{font-size:10px;padding:3px 10px;border-radius:4px;letter-spacing:1px;font-weight:600;font-family:'JetBrains Mono',monospace}
.tag-live{background:var(--red-bg);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.tag-ok{background:var(--green-bg);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.card{background:var(--bg3);border:1px solid var(--border);border-radius:8px;overflow:hidden;margin-bottom:20px}
.card-hdr{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.card-title{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--text2)}
.card-body{padding:20px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}.span2{grid-column:1/-1}
.judgment{font-family:'Newsreader',Georgia,serif;font-size:16px;line-height:1.8;padding:24px;background:var(--bg3);border:1px solid var(--border);border-left:3px solid var(--blue);border-radius:0 8px 8px 0;margin-bottom:20px}
.judgment em{color:var(--amber);font-style:italic}
.overnight{font-size:15px;line-height:1.7;color:var(--text2);padding:20px;background:var(--bg3);border:1px solid var(--border);border-radius:8px;margin-bottom:20px}
.ag{display:flex;gap:14px;padding:14px 0;border-bottom:1px solid rgba(26,32,53,.5)}.ag:last-child{border-bottom:none}
.ag-dot{width:8px;height:8px;border-radius:50%;margin-top:6px;flex-shrink:0}
.ag-dot.critical{background:var(--red);box-shadow:0 0 8px rgba(239,68,68,.4)}.ag-dot.notable{background:var(--amber)}.ag-dot.routine{background:var(--blue)}
.ag-t{font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:600;letter-spacing:1px;text-transform:uppercase;margin-bottom:4px}
.ag-b{font-size:14px;color:var(--text2);line-height:1.6}
.mkt{display:flex;align-items:center;justify-content:space-between;padding:14px 0;border-bottom:1px solid rgba(26,32,53,.5);gap:16px}.mkt:last-child{border-bottom:none}
.mkt-info{flex:1;min-width:0}.mkt-q{font-size:14px;color:var(--text);display:block;margin-bottom:2px}
.mkt-meta{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text4)}
.mkt-right{text-align:right;flex-shrink:0}
.mkt-prob{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:22px}
.delta{font-family:'JetBrains Mono',monospace;font-size:10px;display:block;margin-top:2px}
.cat-badge{font-family:'JetBrains Mono',monospace;font-size:9px;font-weight:600;letter-spacing:1px;padding:2px 7px;border-radius:3px;margin-right:6px;background:color-mix(in srgb,var(--c) 12%,transparent);color:var(--c);display:inline-block;margin-bottom:4px}
.ac-wrap{max-height:400px;overflow-y:auto;padding:0}
.ac-table{width:100%;border-collapse:collapse;font-size:13px}
.ac-table th{font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;letter-spacing:1.5px;text-transform:uppercase;color:var(--text4);text-align:left;padding:8px 10px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg3);z-index:1}
.ac-table td{padding:8px 10px;border-bottom:1px solid rgba(26,32,53,.4);color:var(--text2)}
.ac-table tr.new td{color:var(--text)}.ac-label{font-family:'JetBrains Mono',monospace;font-weight:600;font-size:12px;color:var(--cyan)}
.ac-role{font-size:12px;color:var(--text3)}.ac-alt{font-family:'JetBrains Mono',monospace;font-size:12px;text-align:right}
.baseline-alert{font-family:'JetBrains Mono',monospace;font-size:12px;padding:4px 12px;border-radius:4px;display:inline-block}
.baseline-alert.high{background:var(--red-bg);color:var(--red)}.baseline-alert.moderate{background:var(--amber-bg);color:var(--amber)}
.baseline-normal{font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--text3)}
.centcom-item{padding:10px 0;border-bottom:1px solid rgba(26,32,53,.4);font-size:13px;color:var(--text2)}.centcom-item:last-child{border-bottom:none}
.feeds{display:flex;flex-wrap:wrap;gap:12px;padding:16px 0}
.feed{font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text4);display:flex;align-items:center;gap:6px}
.fd{width:6px;height:6px;border-radius:50%}.fd.ok{background:var(--green)}.fd.err{background:var(--red)}
.foot{border-top:1px solid var(--border);padding:20px 0;margin-top:40px;font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text4);text-align:center;line-height:2}
.map-wrap{position:relative;width:100%;height:420px;background:var(--bg);border-radius:0;overflow:hidden}
.map-wrap canvas{width:100%;height:100%}
#tooltip{position:absolute;background:var(--bg4);border:1px solid var(--border2);border-radius:6px;padding:12px 16px;font-family:'JetBrains Mono',monospace;font-size:11px;pointer-events:none;opacity:0;transition:opacity .15s;z-index:10;max-width:240px;box-shadow:0 8px 32px rgba(0,0,0,.5)}
.map-note{font-size:11px;color:var(--text4);padding:8px 12px;text-align:center}
@media(max-width:768px){.grid2{grid-template-columns:1fr}.hdr-inner{padding:12px 16px}.main{padding:16px 16px 80px}.threat{flex-direction:column;gap:12px}.brand-text{font-size:14px;letter-spacing:3px}.mkt{flex-direction:column;align-items:flex-start;gap:8px}.mkt-right{text-align:left;display:flex;align-items:center;gap:12px}.mkt-prob{font-size:18px}.map-wrap{height:280px}.ac-table{font-size:12px}.judgment{font-size:15px;padding:16px}.sec{font-size:10px}}
@media(max-width:480px){.brand-text{font-size:12px;letter-spacing:2px}.map-wrap{height:220px}}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:var(--bg)}::-webkit-scrollbar-thumb{background:var(--border2);border-radius:3px}
</style></head><body>
<div class="hdr"><div class="hdr-inner"><div class="brand"><div class="pulse"></div><div class="brand-text">Iran Watch</div></div><div class="hdr-right"><strong>{{DATE}}</strong>Updated {{TIME}} &middot; 6-hour cycle</div></div></div>
<div class="main">
<div class="threat {{TL}}"><div class="tl-badge {{TL}}">{{THREAT_LEVEL}}</div><div class="threat-body"><p>{{THREAT_SUMMARY}}</p><div class="scale"><span style="background:rgba(34,197,94,.15);color:var(--green)">ROUTINE</span> <span style="background:rgba(59,130,246,.15);color:var(--blue)">ELEVATED</span> <span style="background:rgba(245,158,11,.15);color:var(--amber)">HIGH</span> <span style="background:rgba(239,68,68,.15);color:var(--red)">CRITICAL</span></div></div></div>
<div class="judgment">{{KEY_JUDGMENT}}</div>
<div class="sec"><span>Latest Update</span></div>
<div class="overnight">{{OVERNIGHT}}</div>
<div class="sec"><span>Activity Feed</span></div>
<div class="card"><div class="card-body">{{GROUPS}}</div></div>
<div class="grid2">
<div class="span2"><div class="sec"><span>Forecasting Panel</span><span class="tag tag-live">Live</span></div><div class="card"><div class="card-body"><div style="font-size:14px;color:var(--text2);margin-bottom:16px;line-height:1.6">{{MARKETS_SUMMARY}}</div>{{MARKETS_HTML}}</div></div></div>
<div><div class="sec"><span>Naval Forces</span></div><div class="card"><div class="card-body"><div style="font-size:14px;color:var(--text2);line-height:1.7">{{NAVAL_SUMMARY}}</div><div style="font-family:'JetBrains Mono',monospace;font-size:10px;color:var(--text4);margin-top:12px">USNI Fleet Tracker &middot; {{NAVAL_DATE}}</div></div></div></div>
<div><div class="sec"><span>Diplomatic Context</span></div><div class="card"><div class="card-body"><div style="font-size:14px;color:var(--text2);line-height:1.7">{{DIPLOMATIC}}</div></div></div></div>
</div>
<div class="sec"><span>Aircraft Detection</span><span class="tag tag-live">{{MIL_COUNT}} aircraft</span></div>
<div class="card">
<div class="card-hdr"><div class="card-title">Military Aircraft &mdash; ADS-B</div><div>{{BASELINE}}</div></div>
<div class="map-wrap" id="mapWrap"><canvas id="mapCanvas"></canvas><div id="tooltip"></div></div>
<div class="map-note">Bright = new &middot; Dim = still present &middot; Most military flights fly dark</div>
<div class="ac-wrap"><table class="ac-table"><thead><tr><th>Callsign</th><th>Type</th><th>Role</th><th>Location</th><th style="text-align:right">Alt</th></tr></thead><tbody>{{MIL_HTML}}</tbody></table></div>
</div>
<div class="sec"><span>Indicators &amp; Warnings</span></div>
<div class="card"><div class="card-body" style="font-size:14px;color:var(--text2);line-height:1.7">{{IW}}</div></div>
<div class="sec"><span>CENTCOM Releases</span></div>
<div class="card"><div class="card-body">{{CENTCOM_HTML}}</div></div>
<div class="feeds">{{FEEDS}}</div>
<div class="foot">Iran Watch &middot; Data: airplanes.live &middot; Polymarket &middot; Kalshi &middot; Metaculus &middot; USNI News &middot; CENTCOM<br>Analysis: Claude Haiku 4.5 &middot; Updated every 6 hours via GitHub Actions</div>
</div>

<script>
(function(){
const aircraft={{AIRCRAFT_JSON}};
const bases=[{name:"Al Udeid AB",lat:25.117,lon:51.315},{name:"Al Dhafra AB",lat:24.248,lon:54.547},{name:"Ali Al Salem",lat:29.346,lon:47.521},{name:"Incirlik AB",lat:37.002,lon:35.426},{name:"RAF Akrotiri",lat:34.590,lon:32.988},{name:"Camp Lemonnier",lat:11.547,lon:43.155}];
const borders={"Iran":[[25.1,61.6],[25.3,58.9],[26.3,56.3],[27.2,54.7],[26.5,53.4],[27.0,51.5],[29.8,50.3],[30.4,48.8],[31.0,47.7],[32.3,47.4],[33.7,46.0],[35.1,45.4],[36.6,45.0],[37.4,44.8],[38.3,44.4],[39.4,44.0],[39.8,47.8],[39.3,48.0],[38.9,48.9],[37.6,49.1],[37.3,50.1],[36.7,53.9],[37.4,55.4],[37.3,57.2],[35.8,60.5],[34.5,60.9],[33.7,60.5],[31.3,61.7],[27.2,63.3],[25.1,61.6]],"Iraq":[[29.1,47.4],[30.4,47.0],[31.0,47.7],[32.3,47.4],[33.7,46.0],[35.1,45.4],[36.6,45.0],[37.4,44.8],[37.1,42.4],[36.8,41.0],[33.4,40.9],[32.0,39.0],[30.0,40.0],[29.1,44.7],[29.1,47.4]],"Saudi Arabia":[[16.4,42.7],[17.5,43.4],[18.2,44.2],[19.0,45.0],[20.0,45.0],[21.5,49.0],[22.5,50.8],[24.0,52.0],[24.2,51.6],[25.8,50.8],[27.0,49.6],[28.5,48.4],[29.1,47.4],[29.1,44.7],[28.0,37.0],[25.0,37.5],[20.0,40.0],[17.8,42.0],[16.4,42.7]]};
const labels=[[32,"IRAN",53],[33,"IRAQ",43.5],[24,"S. ARABIA",45],[35,"TURKEY",35],[15,"YEMEN",47]];
const V={cLat:27,cLon:48,s:14};const wrap=document.getElementById('mapWrap');const canvas=document.getElementById('mapCanvas');const ctx=canvas.getContext('2d');const tip=document.getElementById('tooltip');
let W,H;function tX(n){return(n-V.cLon)*V.s+W/2}function tY(t){return(V.cLat-t)*V.s+H/2}
function getCat(cs){cs=(cs||'').toUpperCase();const c=[{p:['RCH','REACH','PACK','DUKE','MOOSE','FRED','CARGO','HERK'],t:'Airlift',c:'#ef4444'},{p:['ETHYL','JULIET','PEARL','STEEL','SHELL','TEAL','NKAC','PKSN','GOLD','BLUE','IRON'],t:'Tanker',c:'#3b82f6'},{p:['HOMER','TOPCT','JAKE','TITAN','FORTE','MAGIC','SNTRY','REDEYE','OLIVE','MAZDA'],t:'ISR/AWACS',c:'#f59e0b'},{p:['DOOM','DEATH','BATT','MYTEE','BONE','VIPER','EAGLE','RAZOR','HAWK','STRIKE','WRATH','BOLT','ASCOT','TABOR'],t:'Strike',c:'#22c55e'}];for(const g of c)for(const px of g.p)if(cs.startsWith(px))return g;return{t:'Military',c:'#94a3b8'}}
function draw(){ctx.clearRect(0,0,W,H);ctx.fillStyle='#080a0f';ctx.fillRect(0,0,W,H);
ctx.strokeStyle='rgba(26,32,53,.4)';ctx.lineWidth=.5;for(let lat=-10;lat<=60;lat+=5){ctx.beginPath();ctx.moveTo(tX(10),tY(lat));ctx.lineTo(tX(85),tY(lat));ctx.stroke()}for(let lon=10;lon<=85;lon+=5){ctx.beginPath();ctx.moveTo(tX(lon),tY(-10));ctx.lineTo(tX(lon),tY(60));ctx.stroke()}
ctx.fillStyle='rgba(17,21,32,.7)';ctx.lineWidth=1;for(const[name,pts]of Object.entries(borders)){ctx.strokeStyle=name==='Iran'?'rgba(239,68,68,.25)':'#1a2035';ctx.lineWidth=name==='Iran'?2:1;ctx.beginPath();pts.forEach((p,i)=>{const x=tX(p[1]),y=tY(p[0]);i===0?ctx.moveTo(x,y):ctx.lineTo(x,y)});ctx.closePath();ctx.fill();ctx.stroke()}
ctx.font='9px JetBrains Mono,monospace';ctx.fillStyle='#2a3450';ctx.textAlign='center';labels.forEach(([lat,t,lon])=>ctx.fillText(t,tX(lon),tY(lat)));
ctx.setLineDash([3,3]);ctx.strokeStyle='#475569';ctx.lineWidth=1;bases.forEach(b=>{const x=tX(b.lon),y=tY(b.lat);ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.stroke();ctx.font='8px JetBrains Mono,monospace';ctx.fillStyle='#475569';ctx.textAlign='left';ctx.fillText(b.name,x+7,y+3)});ctx.setLineDash([]);
const ret=aircraft.filter(a=>a.status==='returning');const nw=aircraft.filter(a=>a.status!=='returning');
ret.forEach(a=>{const x=tX(a.lon),y=tY(a.lat);ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fillStyle='#2a3450';ctx.fill()});
nw.forEach(a=>{const x=tX(a.lon),y=tY(a.lat);const cat=getCat(a.callsign);const g=ctx.createRadialGradient(x,y,0,x,y,18);g.addColorStop(0,cat.c+'30');g.addColorStop(1,cat.c+'00');ctx.fillStyle=g;ctx.beginPath();ctx.arc(x,y,18,0,Math.PI*2);ctx.fill();ctx.beginPath();ctx.arc(x,y,4,0,Math.PI*2);ctx.fillStyle=cat.c+'90';ctx.fill();ctx.strokeStyle=cat.c;ctx.lineWidth=1.5;ctx.stroke();ctx.font='bold 9px JetBrains Mono,monospace';ctx.fillStyle=cat.c;ctx.textAlign='left';const lbl=a.callsign||a.registration||a.hex||'';ctx.fillText(lbl,x+9,y-3);ctx.font='8px JetBrains Mono,monospace';ctx.fillStyle='#94a3b8';ctx.fillText(a.airframe||cat.t,x+9,y+8)})}
function resize(){W=wrap.clientWidth;H=wrap.clientHeight;canvas.width=W*devicePixelRatio;canvas.height=H*devicePixelRatio;canvas.style.width=W+'px';canvas.style.height=H+'px';ctx.setTransform(devicePixelRatio,0,0,devicePixelRatio,0,0);draw()}resize();window.addEventListener('resize',resize);
let hov=null;wrap.addEventListener('mousemove',e=>{const r=canvas.getBoundingClientRect(),mx=e.clientX-r.left,my=e.clientY-r.top;let found=null;for(const a of aircraft){if(Math.hypot(mx-tX(a.lon),my-tY(a.lat))<16){found=a;break}}
if(found&&found!==hov){hov=found;let h='<div style="font-weight:600;color:var(--cyan);font-size:12px">'+(found.callsign||found.registration||found.hex)+'</div>';if(found.airframe)h+='<div style="color:var(--text);margin-top:3px">'+found.airframe+'</div>';if(found.role)h+='<div style="color:var(--text2);font-size:10px">'+found.role+'</div>';if(found.location_desc)h+='<div style="color:var(--text3);margin-top:3px;font-size:10px">'+found.location_desc+'</div>';if(found.alt_ft)h+='<div style="color:var(--text4);font-size:10px">'+found.alt_ft.toLocaleString()+' ft'+(found.origin?' · '+found.origin:'')+'</div>';tip.innerHTML=h;tip.style.left=Math.min(mx+16,W-240)+'px';tip.style.top=(my-10)+'px';tip.style.opacity='1'}else if(!found){hov=null;tip.style.opacity='0'}});
wrap.addEventListener('mouseleave',()=>{hov=null;tip.style.opacity='0'})})();
</script></body></html>"""


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print(f"IRAN WATCH — Update ({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    print("=" * 60)

    # 0. Load history
    snapshots = load_history(days=7)
    prev_callsigns = set()
    if snapshots:
        latest = snapshots[-1]
        prev_callsigns = set(latest.get("callsigns", []))
        print(f"[History] Previous snapshot has {len(prev_callsigns)} callsigns")

    # 1. Fetch all data sources in parallel-ish order
    aircraft = fetch_aircraft()
    polymarket = fetch_polymarket()
    kalshi = fetch_kalshi()
    metaculus = fetch_metaculus()
    centcom = fetch_centcom_rss()
    naval = fetch_naval()
    diplomatic = fetch_diplomatic_context()

    # 2. Tag aircraft as new/returning
    current_callsigns = set()
    for ac in aircraft.get("mil_aircraft", []):
        cs = ac.get("callsign") or ac.get("hex", "")
        current_callsigns.add(cs)
        ac["status"] = "returning" if cs in prev_callsigns else "new"

    # 3. Compute trends
    all_markets = polymarket.get("markets", []) + kalshi.get("markets", [])
    market_trends = compute_trends(snapshots, all_markets)
    ac_baseline = compute_aircraft_baseline(snapshots)

    # 4. Generate analysis
    analysis = generate_analysis(aircraft, polymarket, metaculus, centcom, naval, diplomatic, kalshi, ac_baseline, market_trends)

    # 5. Generate HTML
    html = generate_html(analysis, aircraft, polymarket, metaculus, centcom, naval, kalshi, market_trends, ac_baseline, snapshots)

    # 6. Write output
    output_path = os.path.join(os.path.dirname(__file__) or ".", "index.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    # 7. Save snapshot for future comparisons
    snapshot_data = {
        "callsigns": list(current_callsigns),
        "mil_count": aircraft.get("mil_count", 0),
        "markets": [{"question": m["question"], "probability": m["probability"],
                     "volume": m.get("volume", "0")} for m in all_markets],
    }
    save_snapshot(snapshot_data)
    cleanup_old_history()

    print(f"\n{'='*60}")
    print(f"[Done] Written to {output_path}")
    print(f"  Aircraft: {aircraft['status']} ({aircraft.get('mil_count',0)} mil)")
    print(f"  Polymarket: {polymarket['status']} ({len(polymarket.get('markets',[]))} markets)")
    print(f"  Kalshi: {kalshi['status']} ({len(kalshi.get('markets',[]))} markets)")
    print(f"  Metaculus: {metaculus['status']} ({len(metaculus.get('questions',[]))} questions)")
    print(f"  Naval: {naval['status']} ({len(naval.get('carriers',[]))} carriers)")
    print(f"  CENTCOM: {centcom['status']} ({len(centcom.get('releases',[]))} releases)")
    print(f"  Diplomatic: {diplomatic['status']}")
    print(f"  Claude: {'API key present' if ANTHROPIC_API_KEY else 'NO API KEY'}")


if __name__ == "__main__":
    main()
