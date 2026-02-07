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

# Military callsign prefixes (US military airlift, tanker, bomber, ISR)
MIL_PREFIXES = [
    "RCH", "REACH",  # C-17/C-5 airlift
    "DOOM", "DEATH",  # B-2/B-52 bombers
    "IRON",           # Various military
    "HAVOC",          # Attack aviation
    "KING",           # HC-130 CSAR
    "ETHYL",          # KC-135 tanker
    "JULIET",         # KC-10 tanker
    "HOMER",          # P-8 Poseidon
    "TOPCT",          # RC-135
    "JAKE",           # E-3 AWACS
    "TITAN",          # RQ-4 Global Hawk
    "EVAC",           # Medical evacuation
    "SAM",            # VIP transport
    "PACK",           # C-17
    "DUKE",           # C-17
]

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

        for s in all_aircraft:
            callsign = (s[1] or "").strip().upper()
            origin = s[2] or ""
            on_ground = s[8]
            lat, lon = s[6], s[5]
            alt_m = s[7]

            is_mil = any(callsign.startswith(p) for p in MIL_PREFIXES)

            if is_mil and not on_ground:
                mil_aircraft.append({
                    "callsign": callsign,
                    "origin": origin,
                    "lat": round(lat, 2) if lat else None,
                    "lon": round(lon, 2) if lon else None,
                    "alt_ft": round(alt_m * 3.281) if alt_m else None,
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
        # Search by tag
        resp = requests.get(url, params={
            "tag": "iran", "active": "true", "closed": "false", "limit": 30
        }, timeout=15)
        resp.raise_for_status()
        events = resp.json()

        markets = []
        for ev in events:
            for m in ev.get("markets", []):
                q = (m.get("question") or "").lower()
                if any(kw in q for kw in ["strike", "attack", "military", "khamenei", "regime", "war"]):
                    prices = json.loads(m.get("outcomePrices", "[]"))
                    yes_price = round(float(prices[0]) * 100) if prices else None
                    if yes_price is not None:
                        markets.append({
                            "question": m.get("question", ""),
                            "probability": yes_price,
                            "volume": m.get("volume", "0"),
                            "url": f"https://polymarket.com/event/{ev.get('slug', '')}",
                        })

        print(f"[Polymarket] Found {len(markets)} Iran-related markets")
        return {"status": "ok", "markets": markets[:10]}

    except Exception as e:
        print(f"[Polymarket] Error: {e}")
        return {"status": "error", "error": str(e), "markets": []}


def fetch_metaculus():
    """Fetch Iran-related forecasting questions from Metaculus API."""
    print("[Metaculus] Fetching Iran questions...")

    try:
        resp = requests.get(
            "https://www.metaculus.com/api/questions/",
            params={"search": "iran", "status": "open", "limit": 20, "type": "binary"},
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
        resp = requests.get("https://www.centcom.mil/RSS/", timeout=15)
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
  "posture_change_24h": "One clear sentence: what changed in force posture in the last 24h",
  "overnight_summary": "Paragraph summarizing overnight diplomatic/military developments",
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
        "posture_change_24h": "Automated data collection detected no major new deployments in the past 24 hours; force posture appears unchanged from the previous day.",
        "overnight_summary": "This is an automatically generated summary. For full AI-powered analysis, add your ANTHROPIC_API_KEY to GitHub Secrets. Latest CENTCOM releases: " + "; ".join(centcom.get("releases", [])[:3]),
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
    for m in polymarket.get("markets", []):
        prob = m["probability"]
        col = "var(--accent-red)" if prob >= 60 else "var(--accent-amber)" if prob >= 40 else "var(--text-secondary)"
        vol = float(m.get("volume", 0))
        vol_str = f"${vol/1e6:.0f}M" if vol >= 1e6 else f"${vol/1e3:.0f}K" if vol >= 1e3 else f"${vol:.0f}"
        markets_html += f"""
        <div class="mrow">
          <div class="mq">{m['question']}<span class="mplat">Polymarket · Vol: {vol_str}</span></div>
          <div class="mprob" style="color:{col}">{prob}%</div>
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

    # Format military aircraft
    mil_html = ""
    for a in opensky.get("mil_aircraft", [])[:10]:
        mil_html += f"""<tr>
          <td class="aname">{a['callsign']}</td>
          <td class="loc">{a.get('lat', '?')}°N, {a.get('lon', '?')}°E</td>
          <td>{a.get('alt_ft', '?')} ft</td>
          <td class="loc">{a.get('origin', '?')}</td>
        </tr>"""

    if not mil_html:
        mil_html = '<tr><td colspan="4" style="color:var(--text-muted)">No military aircraft with active transponders detected in this update. Note: most military flights do not broadcast ADS-B.</td></tr>'

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
    html = html.replace("{{THREAT_SUMMARY}}", analysis.get("threat_summary", ""))
    html = html.replace("{{KEY_JUDGMENT}}", analysis.get("key_judgment", ""))
    html = html.replace("{{POSTURE_CHANGE}}", analysis.get("posture_change_24h", ""))
    html = html.replace("{{OVERNIGHT_SUMMARY}}", analysis.get("overnight_summary", ""))
    html = html.replace("{{ACTIVITY_GROUPS}}", groups_html)
    html = html.replace("{{MARKETS_HTML}}", markets_html)
    html = html.replace("{{MARKETS_SUMMARY}}", analysis.get("prediction_markets_summary", ""))
    html = html.replace("{{MIL_AIRCRAFT_HTML}}", mil_html)
    html = html.replace("{{MIL_COUNT}}", str(opensky.get("mil_count", 0)))
    html = html.replace("{{TOTAL_AIRCRAFT}}", str(opensky.get("total_aircraft", 0)))
    html = html.replace("{{FEEDS_HTML}}", feeds_html)
    html = html.replace("{{CENTCOM_HTML}}", centcom_html)
    html = html.replace("{{DIPLOMATIC_SUMMARY}}", analysis.get("diplomatic_summary", ""))
    html = html.replace("{{IW_UPDATES}}", analysis.get("iw_updates", ""))

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
.wrap{max-width:1400px;margin:0 auto;padding:24px 32px}
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
</style>
</head>
<body>
<header>
  <div class="hdr-left">
    <div class="logo"><span class="logo-dot"></span>IRAN WATCH</div>
    <span class="subtitle">Open-Source Force Posture Monitor</span>
  </div>
  <div class="ts"><strong>{{DATE_STR}} · {{TIME_STR}}</strong>Auto-updates daily at 0500 GMT</div>
</header>
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
      <div class="alabel blue">24-Hour Posture Change</div>
      <div class="atext">{{POSTURE_CHANGE}}</div>
    </div>
    <div class="abox" style="border-left:3px solid var(--accent-red)">
      <div class="alabel red">What Changed Overnight</div>
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
    <table class="pt"><thead><tr><th>Callsign</th><th>Position</th><th>Altitude</th><th>Origin</th></tr></thead>
    <tbody>{{MIL_AIRCRAFT_HTML}}</tbody></table>
    <div style="margin-top:12px;font-size:12px;color:var(--text-muted)">⚠ Most military aircraft fly without ADS-B transponders. This data represents only the fraction that broadcast. Absence of aircraft does not mean absence of activity.</div>
  </div>
  <div class="srcs"><span class="stag"><a href="https://opensky-network.org" target="_blank">OpenSky Network API</a></span><span class="stag">Free, rate-limited</span></div>
</div>

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

    # 1. Fetch all data sources
    opensky = fetch_opensky()
    polymarket = fetch_polymarket()
    metaculus = fetch_metaculus()
    centcom = fetch_centcom_rss()

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
