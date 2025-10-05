import os, time, json, ssl, socket, datetime, requests
from pathlib import Path
from bs4 import BeautifulSoup

# === Konfig per ENV ===
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
ALERT_ROLE_ID = os.environ.get("ALERT_ROLE_ID", "").strip()  # optional: Role-Ping bei Statuswechsel

REGION_HOSTS = {
    "EU":   ["eu.actual.battle.net", "overwatch.blizzard.com"],
    "NA":   ["us.actual.battle.net", "overwatch.blizzard.com"],
    "ASIA": ["kr.actual.battle.net", "overwatch.blizzard.com"],
}
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
KNOWN_ISSUES_URL = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"
OAUTH_URL = "https://oauth.battle.net/authorize?response_type=code&client_id=dummy&redirect_uri=https://example.com"

INFO_MS = float(os.environ.get("INFO_MS", "200"))     # ab hier INFO
WARN_MS = float(os.environ.get("WARN_MS", "400"))     # ab hier WARN
CERT_WARN_DAYS = int(os.environ.get("CERT_WARN_DAYS", "14"))

STATE_DIR = Path(".bot_state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE   = STATE_DIR / "ow_message_id.txt"
HIST_FILE  = STATE_DIR / "history.json"
LAST_FILE  = STATE_DIR / "last_payload.json"
TREND_FILE = STATE_DIR / "last_latency.json"
SPARK_PATH = Path("assets/sparkline.png")

def _now_utc_str(): return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')

# --- Messungen ---
def dns_time_ms(host):
    t0 = time.time()
    try:
        socket.getaddrinfo(host, 443)
        return round((time.time()-t0)*1000.0, 1)
    except OSError:
        return None

def tcp_handshake_ms(host, port=443, timeout=3.0):
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.time()-t0)*1000.0, 1)
    except OSError:
        return None

def http_check(url, expect=(200, 301, 302, 303, 307, 308), timeout=6):
    try:
        r = requests.head(url, timeout=timeout, allow_redirects=True)
        return r.status_code in expect, r.status_code
    except Exception:
        return False, None

def cert_days_left(host, port=443, timeout=5):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        exp = datetime.datetime.strptime(cert['notAfter'], "%b %d %H:%M:%S %Y %Z")
        return (exp - datetime.datetime.utcnow()).days
    except Exception:
        return None

def aggregate_region(region, hosts):
    ms = [m for h in hosts if (m:=tcp_handshake_ms(h)) is not None]
    if not ms: return {"min":None,"avg":None,"max":None}
    return {"min":min(ms),"avg":round(sum(ms)/len(ms),1),"max":max(ms)}

# --- Quellen ---
def maintenance_status():
    try:
        html = requests.get(MAINT_URL, timeout=20).text
        txt = BeautifulSoup(html, "html.parser").get_text(" ").lower()
        if "overwatch" in txt and any(k in txt for k in ["maintenance","downtime","scheduled"]):
            return "warn", "Wartungshinweise gefunden."
        return "ok", "Keine expliziten OW-Wartungshinweise gefunden."
    except Exception as e:
        return "unknown", f"Wartungsseite nicht prüfbar ({e})."

def known_issues_status():
    try:
        r = requests.get(KNOWN_ISSUES_URL, timeout=12)
        if r.status_code == 200:
            return "info", "Bekannte Probleme gelistet (Details im Link)."
        elif r.status_code in (301,302,303,307,308):
            return "info", "Forum erreichbar (weitergeleitet)."
        else:
            return "unknown", "Forum nicht erreichbar."
    except Exception:
        return "unknown", "Forum nicht erreichbar."

# --- Bewertung / State ---
def severity_from_latency(avg):
    if avg is None: return "unknown"
    if avg >= WARN_MS: return "warn"
    if avg >= INFO_MS: return "info"
    return "ok"

def worst_state(states):
    order = {"ok":0,"info":1,"warn":2,"unknown":3}
    return max(states, key=lambda s: order[s])

def color_for(state):
    return {"ok":0x2ECC71,"info":0x3498DB,"warn":0xF1C40F,"unknown":0x95A5A6}[state]

def read_json(p:Path, default):
    try: return json.loads(p.read_text())
    except Exception: return default

def write_json(p:Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":")))

# --- Verlauf / Sparkline ---
def append_history(is_ok: bool):
    hist = read_json(HIST_FILE, [])
    hist.append({"t": int(time.time()), "ok": 1 if is_ok else 0})
    hist = hist[-168:]  # 7 Tage bei stündlich
    write_json(HIST_FILE, hist)
    return hist

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
        d.line(pts, width=2, fill=(120,180,250))
        ok24 = sum(x["ok"] for x in hist[-24:]) / max(1,len(hist[-24:])) * 100
        ok168 = sum(x["ok"] for x in hist) / len(hist) * 100
        d.text((10,h-14), f"Uptime 24h: {ok24:.0f}% • 7T: {ok168:.0f}%", fill=(180,180,180))
        SPARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(SPARK_PATH)
    except Exception:
        pass

# --- Discord I/O ---
def send_new(payload):
    r = requests.post(WEBHOOK+"?wait=true", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["id"]

def edit_existing(mid, payload):
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    i = parts.index("webhooks"); wid, tok = parts[i+1], parts[i+2]
    url = f"https://discord.com/api/webhooks/{wid}/{tok}/messages/{mid}"
    return requests.patch(url, json=payload, timeout=20)

def maybe_alert_ping(new_state, old_state):
    if not ALERT_ROLE_ID: return
    if old_state == "ok" and new_state in ("warn","unknown"):
        content = f"<@&{ALERT_ROLE_ID}> Statuswechsel: **{old_state.upper()} → {new_state.upper()}**"
        try: requests.post(WEBHOOK, json={"content":content, "allowed_mentions":{"parse":["roles"]}}, timeout=10)
        except Exception: pass

# --- Main ---
if __name__ == "__main__":
    # Messungen
    maint_state, maint_msg = maintenance_status()
    issues_state, issues_msg = known_issues_status()
    http_ok, http_code = http_check(OAUTH_URL)
    cert_days = cert_days_left("overwatch.blizzard.com")

    regions = {}
    for r, hosts in REGION_HOSTS.items():
        regions[r] = aggregate_region(r, hosts)

    # Trendvergleich
    last_lat = read_json(TREND_FILE, {})
    trends = {}
    for r, vals in regions.items():
        prev = last_lat.get(r, {}).get("avg")
        cur  = vals["avg"]
        if prev is None or cur is None: trends[r] = "•"
        else: trends[r] = "▲" if cur>prev+20 else ("▼" if cur<prev-20 else "→")
    write_json(TREND_FILE, regions)

    # Bewerteter State
    parts = [maint_state, issues_state]
    for r, vals in regions.items(): parts.append(severity_from_latency(vals["avg"]))
    if not http_ok: parts.append("info")  # OAuth nicht erwartungsgemäß
    if cert_days is not None and cert_days < CERT_WARN_DAYS: parts.append("info")
    new_state = worst_state(parts)

    # Verlauf & Sparkline
    hist = append_history(new_state == "ok")
    render_sparkline(hist)

    # Embed aufbauen
    fields=[]
    for r, vals in regions.items():
        if vals["avg"] is None: v = "keine Messung"
        else: v = f'Ø {vals["avg"]} ms ({vals["min"]}/{vals["max"]}) {trends[r]}'
        fields.append({"name": f"{r} – Erreichbarkeit", "value": v, "inline": True})
    fields.append({"name":"Wartung", "value": f"[{maint_msg}]({MAINT_URL})", "inline": False})
    fields.append({"name":"Known Issues", "value": f"[{issues_msg}]({KNOWN_ISSUES_URL})", "inline": False})
    if not http_ok:
        fields.append({"name":"Login/OAuth", "value": f"Unerwarteter Status {http_code}", "inline": True})
    if cert_days is not None:
        fields.append({"name":"TLS-Zertifikat", "value": f"{cert_days} Tage gültig", "inline": True})

    embed = {
        "title": "Overwatch 2 – Status",
        "description": "Heuristische Erreichbarkeit & Hinweise (kein offizieller Live-Status).",
        "color": color_for(new_state),
        "fields": fields,
        "footer": {"text": f"Letzte Prüfung: {_now_utc_str()}"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    if THUMB_URL: embed["thumbnail"] = {"url": THUMB_URL}
    if SPARK_PATH.exists():
        repo = os.environ.get("GITHUB_REPOSITORY")
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{repo}/main/assets/sparkline.png"}

    components = [{"type":1,"components":[
        {"type":2,"style":5,"label":"Maintenance","url":MAINT_URL},
        {"type":2,"style":5,"label":"Known Issues","url":KNOWN_ISSUES_URL},
        {"type":2,"style":5,"label":"Support","url":"https://support.blizzard.com"}
    ]}]

    payload = {"embeds":[embed], "components":components}

    # Diff-Only
    last = read_json(LAST_FILE, None)
    if last == payload: raise SystemExit(0)
    write_json(LAST_FILE, payload)

    # Editieren/Erstellen
    old_state = read_json(STATE_DIR/"state.json", {"state":"ok"})["state"]
    mid = MID_FILE.read_text().strip() if MID_FILE.exists() else None
    if mid:
        r = edit_existing(mid, payload)
        if r.status_code == 404: mid = None
        else: r.raise_for_status()
    if not mid:
        new_id = send_new(payload)
        MID_FILE.write_text(str(new_id))

    # Optionaler Ping bei Statuswechsel
    if old_state != new_state:
        maybe_alert_ping(new_state, old_state)
        write_json(STATE_DIR/"state.json", {"state":new_state})