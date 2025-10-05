# scripts/ow_status.py
import os, time, json, socket, datetime, re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# =========================
# Konfiguration / ENV
# =========================
WEBHOOK   = os.environ["DISCORD_WEBHOOK_URL"].strip()
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
REGIONS   = [r.strip() for r in os.environ.get("REGIONS", "EU,NA,ASIA").split(",") if r.strip()]

# Region → Hosts für TCP-Handshake (443)
REGION_HOSTS = {
    "EU":   ["eu.actual.battle.net", "overwatch.blizzard.com"],
    "NA":   ["us.actual.battle.net", "overwatch.blizzard.com"],
    "ASIA": ["kr.actual.battle.net", "overwatch.blizzard.com"],
}
REGIONS = [r for r in REGIONS if r in REGION_HOSTS] or ["EU","NA","ASIA"]

# Quellen (ohne Login)
MAINT_URL       = "https://eu.support.blizzard.com/en/article/000358479"
KNOWN_ISSUES_JSON = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64.json"

# Plattform-Statusseiten (ohne Login, Text-Heuristik)
PS_STATUS_URL = "https://status.playstation.com"
XBOX_STATUS_URL = "https://support.xbox.com/en-US/xbox-live-status"
NIN_STATUS_URL = "https://www.nintendo.co.jp/netinfo/en_US/index.html"

# Schwellen (nur optisch für OK/INFO/WARN bei Latenz)
INFO_MS = float(os.environ.get("INFO_MS", "200"))
WARN_MS = float(os.environ.get("WARN_MS", "400"))

# State/Assets
STATE_DIR  = Path(".bot_state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE   = STATE_DIR / "ow_message_id.txt"
LAST_FILE  = STATE_DIR / "last_payload.json"
HIST_FILE  = STATE_DIR / "history.json"
LAT_FILE   = STATE_DIR / "last_latency.json"
STATE_FILE = STATE_DIR / "state.json"
CHANGELOG  = STATE_DIR / "changelog.json"

SPARK_PATH = Path("assets/sparkline.png")
REPO       = os.environ.get("GITHUB_REPOSITORY", "")

UA = {"User-Agent": "OW2-Status/1.3 (+github-actions)"}

# Farben & Ordnung
COLORS = {"ok": 0x2ECC71, "info": 0x3498DB, "warn": 0xF1C40F, "unknown": 0x95A5A6}
ORDER  = {"ok":0, "info":1, "warn":2, "unknown":3}

# =========================
# Utility / State
# =========================
def now_utc():     return datetime.datetime.now(datetime.UTC)
def now_utc_str(): return now_utc().strftime("%Y-%m-%d %H:%M UTC")

def read_json(p: Path, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":")), encoding="utf-8")

# =========================
# Messungen: TCP/DNS/HTTP
# =========================
def tcp_ms(host, port=443, timeout=3.0):
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.time()-t0)*1000.0, 1)
    except OSError:
        return None

def aggregate_region(hosts):
    vals = [m for h in hosts if (m := tcp_ms(h)) is not None]
    if not vals: return {"min":None,"avg":None,"max":None}
    return {"min":min(vals), "avg":round(sum(vals)/len(vals),1), "max":max(vals)}

def severity_from_latency(avg):
    if avg is None:        return "unknown"
    if avg >= WARN_MS:     return "warn"
    if avg >= INFO_MS:     return "info"
    return "ok"

def worst_state(states):
    return max(states, key=lambda s: ORDER.get(s,0))

# =========================
# Quellen (ohne Login)
# =========================
def fetch_known_issues_summary():
    """Discourse-JSON: (count_24h, last_title, last_url). Robust & ohne Login."""
    try:
        r = requests.get(KNOWN_ISSUES_JSON, timeout=12, headers=UA)
        r.raise_for_status()
        data = r.json()
        topics = data.get("topic_list", {}).get("topics", [])
        now_ts = time.time()
        day_ago = now_ts - 24*3600
        cnt_24h, last_title, last_slug, last_id, last_ts = 0, None, None, None, 0.0
        for t in topics:
            t_iso = t.get("last_posted_at") or t.get("created_at") or t.get("bumped_at")
            if not t_iso: continue
            try:
                ts = datetime.datetime.fromisoformat(t_iso.replace("Z","+00:00")).timestamp()
            except Exception:
                ts = 0.0
            if ts >= day_ago: cnt_24h += 1
            if ts > last_ts:
                last_ts   = ts
                last_title= t.get("title")
                last_slug = t.get("slug")
                last_id   = t.get("id")
        last_url = f"https://us.forums.blizzard.com/en/overwatch/t/{last_slug}/{last_id}" if last_slug and last_id else "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"
        return cnt_24h, (last_title or "—"), last_url
    except Exception:
        return None, None, "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"

MAINT_DATE_RE = re.compile(r"(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s*)?(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*[, ]+\s*(\d{4})", re.I)

def fetch_maintenance_hint():
    """Heuristik: erkennt Overwatch + Maintenance-Begriffe. Optional Datum."""
    try:
        html = requests.get(MAINT_URL, timeout=20, headers=UA).text
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ")
        lw = text.lower()
        if "overwatch" in lw and any(k in lw for k in ["maintenance","downtime","scheduled","maintenance schedule"]):
            m = MAINT_DATE_RE.search(text)
            when = f"{m.group(2)} {m.group(3)} {m.group(4)}" if m else "Termin auf Seite"
            return "warn", f"Wartungshinweis gefunden ({when})."
        return "ok", "Keine expliziten OW-Wartungshinweise."
    except Exception:
        return "unknown", "Wartungsseite nicht prüfbar."

# =========================
# Plattform-Status (Heuristik)
# =========================
def fetch_text(url, timeout=10):
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
        r.raise_for_status()
        return (r.text or "").lower()
    except Exception:
        return None

def classify(text, ok_kw, warn_kw, bad_kw):
    if text is None: return "unknown"
    if any(k in text for k in bad_kw):  return "warn"
    if any(k in text for k in warn_kw): return "info"
    if any(k in text for k in ok_kw):   return "ok"
    return "unknown"

def platform_status_overview(pc_overall_state: str):
    res = {}
    # PC = Gesamtstatus deiner Heuristik
    res["PC"] = (pc_overall_state, "Overwatch Web/Reachability", "https://overwatch.blizzard.com")

    # PlayStation
    ps_txt = fetch_text(PS_STATUS_URL)
    res["PlayStation"] = (classify(
        ps_txt,
        ok_kw=["all services are up","no issues","up and running","all services are available"],
        warn_kw=["limited","degraded","some services"],
        bad_kw=["major outage","outage","down","service is down"]
    ), "PSN Service Status", PS_STATUS_URL)

    # Xbox
    xb_txt = fetch_text(XBOX_STATUS_URL)
    res["Xbox"] = (classify(
        xb_txt,
        ok_kw=["all services up","no problems","up and running","all services are available"],
        warn_kw=["limited","degraded","some services"],
        bad_kw=["major outage","outage","down"]
    ), "Xbox Live Status", XBOX_STATUS_URL)

    # Nintendo Switch
    nin_txt = fetch_text(NIN_STATUS_URL)
    res["Switch"] = (classify(
        nin_txt,
        ok_kw=["all servers are operating normally","operating normally","no issues"],
        warn_kw=["maintenance","under maintenance","scheduled maintenance"],
        bad_kw=["currently experiencing issues","service outage","outage","down"]
    ), "Nintendo Online Status", NIN_STATUS_URL)

    return res

def platform_icon(state: str) -> str:
    return {"ok":"🟢","info":"🟡","warn":"🔴","unknown":"⚪️"}.get(state, "⚪️")

# =========================
# Verlauf / Uptime / Sparkline / Changelog
# =========================
def append_history(is_ok: bool):
    hist = read_json(HIST_FILE, [])
    hist.append({"t": int(time.time()), "ok": 1 if is_ok else 0})
    hist = hist[-168:]  # 7 Tage stündlich
    write_json(HIST_FILE, hist)
    return hist

def uptimes(hist):
    if not hist: return (0,0)
    last24 = hist[-24:] if len(hist)>=24 else hist
    u24 = round(sum(x["ok"] for x in last24)/len(last24)*100)
    u7  = round(sum(x["ok"] for x in hist)/len(hist)*100)
    return (u24, u7)

def render_sparkline(hist):
    try:
        from PIL import Image, ImageDraw
        if not hist: return
        w,h = 420,60
        img = Image.new("RGB",(w,h),(24,26,27))
        d = ImageDraw.Draw(img)
        d.rectangle([0,0,w-1,h-1], outline=(60,60,60))
        n = max(2,len(hist)); step = (w-16)/(n-1)
        pts=[]
        for i,e in enumerate(hist):
            y = 10 + (1-e["ok"])*(h-20)
            x = 8 + i*step
            pts.append((x,y))
        d.line(pts, width=2)
        u24,u7 = uptimes(hist)
        d.text((10,h-14), f"Uptime 24h: {u24}% • 7T: {u7}%", fill=(200,200,200))
        SPARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(SPARK_PATH)
    except Exception:
        pass

def save_changelog_change(old_state, new_state):
    if old_state == new_state: return
    cl = read_json(CHANGELOG, [])
    cl.append({"t": now_utc_str(), "from": old_state, "to": new_state})
    write_json(CHANGELOG, cl[-6:])  # letzten 6 Einträge

def last_changelog_lines(n=2):
    cl = read_json(CHANGELOG, [])
    if not cl: return "—"
    lines = [f"{e['t']} → {e['to'].upper()}" for e in cl[-n:]]
    return " • ".join(lines)

# =========================
# Discord I/O
# =========================
def parse_webhook():
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    i = parts.index("webhooks")
    return parts[i+1], parts[i+2]

def discord_request(method, url, json_payload):
    # simples 429-Handling
    for _ in range(4):
        r = requests.request(method, url, json=json_payload, timeout=20, headers=UA)
        if r.status_code != 429:
            return r
        retry = float(r.headers.get("Retry-After", "1"))
        time.sleep(min(max(retry,1.0), 10.0))
    return r

def send_new(payload):
    r = discord_request("POST", WEBHOOK + "?wait=true", payload)
    r.raise_for_status()
    return r.json()["id"]

def edit_existing(mid, payload):
    wid, tok = parse_webhook()
    url = f"https://discord.com/api/webhooks/{wid}/{tok}/messages/{mid}"
    return discord_request("PATCH", url, payload)

# =========================
# Main
# =========================
if __name__ == "__main__":
    # Regionen messen + Trends
    regions = {r: aggregate_region(REGION_HOSTS[r]) for r in REGIONS}
    last_lat = read_json(LAT_FILE, {})
    trends = {}
    for r in REGIONS:
        prev = last_lat.get(r, {}).get("avg")
        cur  = regions[r]["avg"]
        if prev is None or cur is None: trends[r] = "•"
        else: trends[r] = "▲" if cur > (prev + 20) else ("▼" if cur < (prev - 20) else "→")
    write_json(LAT_FILE, regions)

    # Quellen
    maint_state, maint_msg        = fetch_maintenance_hint()
    ki_count, ki_title, ki_url    = fetch_known_issues_summary()

    # Gesamtzustand
    parts = [maint_state]
    parts += [severity_from_latency(regions[r]["avg"]) for r in REGIONS]
    if ki_count is not None and ki_count > 0:
        parts.append("info")  # Aktivität bei Known Issues
    new_state = worst_state(parts)

    # Verlauf / Uptime / Sparkline / Changelog
    hist = append_history(new_state == "ok")
    u24, u7 = uptimes(hist)
    render_sparkline(hist)

    old_state = read_json(STATE_FILE, {"state":"ok"})["state"]
    if old_state != new_state:
        save_changelog_change(old_state, new_state)
        write_json(STATE_FILE, {"state": new_state})

    # Monospace Kopfzeile (EU/NA/ASIA + Uptime)
    head_bits = [ (f"{r} Ø{regions[r]['avg']:.0f}ms" if regions[r]["avg"] is not None else f"{r} n/a") for r in REGIONS ]
    head_line = "   ".join(head_bits) + f" | 24h {u24}% • 7T {u7}%"
    description = f"```\n{head_line}\n```"

    # Plattform-Block (PC/PS/Xbox/Switch)
    platforms = platform_status_overview(new_state)
    lines = []
    for name in ("PC", "PlayStation", "Xbox", "Switch"):
        st, note, link = platforms[name]
        lines.append(f"{name:<11} {platform_icon(st)} {st.upper():<7}")
    platform_block = "```\n" + "\n".join(lines) + "\n```"

    # Felder aufbauen
    fields = []
    # Plattformen ganz oben
    fields.append({"name": "Plattformen", "value": platform_block, "inline": False})

    # Regionen (inline)
    for r in REGIONS:
        v = regions[r]
        val = "keine Messung" if v["avg"] is None else f"Ø {v['avg']} ms ({v['min']}/{v['max']}) {trends[r]}"
        fields.append({"name": f"{r} – Erreichbarkeit", "value": val, "inline": True})

    # Wartung
    fields.append({"name": "Wartung", "value": f"[{maint_msg}]({MAINT_URL})", "inline": False})

    # Known Issues
    if ki_count is None:
        ki_val = f"[Keine Daten]({ki_url})"
    else:
        label = f"{ki_count} neue/aktualisierte Beiträge in 24h" if ki_count>0 else "Keine neuen Beiträge in 24h"
        ki_val = f"[{label}]({ki_url})" + (f"\nZuletzt: „{ki_title}“" if ki_title else "")
    fields.append({"name": "Known Issues", "value": ki_val, "inline": False})

    # Mini-Changelog (letzte 2 Wechsel)
    fields.append({"name": "Letzte Änderungen", "value": last_changelog_lines(2), "inline": False})

    # Embed
    embed = {
        "title": "Overwatch 2 – Status",
        "description": description,   # Kopfzeile (Monospace)
        "color": COLORS.get(new_state, COLORS["unknown"]),
        "fields": fields,
        "footer": {"text": f"Letzte Prüfung: {now_utc_str()}"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    if THUMB_URL:
        embed["thumbnail"] = {"url": THUMB_URL}
    if REPO and SPARK_PATH.exists():
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{REPO}/main/assets/sparkline.png"}

    # Buttons
    components = [{
        "type": 1,
        "components": [
            {"type":2,"style":5,"label":"Maintenance","url":MAINT_URL},
            {"type":2,"style":5,"label":"Known Issues","url":ki_url},
            {"type":2,"style":5,"label":"Support","url":"https://support.blizzard.com"}
        ]
    }]

    payload = {"embeds":[embed], "components":components}

    # Diff-only: nur editieren, wenn sich etwas geändert hat
    last = read_json(LAST_FILE, None)
    if last == payload:
        raise SystemExit(0)
    write_json(LAST_FILE, payload)

    # Nachricht bearbeiten / anlegen
    mid = MID_FILE.read_text().strip() if MID_FILE.exists() else None
    if mid:
        r = edit_existing(mid, payload)
        if r.status_code == 404:
            mid = None
        else:
            r.raise_for_status()
    if not mid:
        new_id = send_new(payload)
        MID_FILE.write_text(str(new_id))