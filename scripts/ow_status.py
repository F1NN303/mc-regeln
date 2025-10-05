import os, time, json, ssl, socket, datetime, math
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------- Konfig ----------
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"].strip()
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
ALERT_ROLE_ID = os.environ.get("ALERT_ROLE_ID", "").strip()  # optional

INFO_MS = float(os.environ.get("INFO_MS", "200"))
WARN_MS = float(os.environ.get("WARN_MS", "400"))
CERT_WARN_DAYS = int(os.environ.get("CERT_WARN_DAYS", "14"))
REPO = os.environ.get("GITHUB_REPOSITORY", "")

STATE_DIR = Path(".bot_state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE   = STATE_DIR / "ow_message_id.txt"
HIST_FILE  = STATE_DIR / "history.json"
LAST_FILE  = STATE_DIR / "last_payload.json"
TREND_FILE = STATE_DIR / "last_latency.json"
STATE_JSON = STATE_DIR / "state.json"
SPARK_PATH = Path("assets/sparkline.png")

# Quellen
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
KNOWN_ISSUES_URL = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"
OAUTH_URL = "https://oauth.battle.net/authorize?response_type=code&client_id=dummy&redirect_uri=https://example.com"

REGION_HOSTS = {
    "EU":   ["eu.actual.battle.net", "overwatch.blizzard.com"],
    "NA":   ["us.actual.battle.net", "overwatch.blizzard.com"],
    "ASIA": ["kr.actual.battle.net", "overwatch.blizzard.com"],
}

UA = {"User-Agent": "OW2-Status/1.1 (+github-actions)"}

# ---------- Utils ----------
def now_utc_str():
    return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')

def read_json(p: Path, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":")), encoding="utf-8")

def bounded(v, lo, hi):
    return max(lo, min(hi, v))

# ---------- Netzwerk: DNS/TCP/HTTP mit Retries ----------
def with_retry(fn, attempts=3, backoff=0.5, fallback=None):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts-1:
                return fallback
            time.sleep(backoff * (2**i))

def dns_time_ms(host):
    def _f():
        t0 = time.time()
        socket.getaddrinfo(host, 443)
        return round((time.time()-t0)*1000.0, 1)
    return with_retry(_f, fallback=None)

def tcp_handshake_ms(host, port=443, timeout=3.0):
    def _f():
        t0 = time.time()
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.time()-t0)*1000.0, 1)
    return with_retry(_f, fallback=None)

def http_head_ok(url, expect=(200,301,302,303,307,308), timeout=6):
    def _f():
        r = requests.head(url, timeout=timeout, allow_redirects=True, headers=UA)
        return (r.status_code in expect, r.status_code)
    return with_retry(_f, fallback=(False, None))

def cert_days_left(host, port=443, timeout=5):
    def _f():
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        exp = datetime.datetime.strptime(cert['notAfter'], "%b %d %H:%M:%S %Y %Z")
        return (exp - datetime.datetime.utcnow()).days
    return with_retry(_f, fallback=None)

# ---------- Quellenchecks ----------
def maintenance_status():
    def _f():
        html = requests.get(MAINT_URL, timeout=20, headers=UA).text
        txt = BeautifulSoup(html, "html.parser").get_text(" ").lower()
        if "overwatch" in txt and any(k in txt for k in ["maintenance","downtime","scheduled"]):
            return "warn", "Wartungshinweise gefunden."
        return "ok", "Keine expliziten OW-Wartungshinweise gefunden."
    try:
        return with_retry(_f, fallback=("unknown", "Wartungsseite nicht prüfbar."))
    except Exception:
        return "unknown", "Wartungsseite nicht prüfbar."

def known_issues_status():
    def _f():
        r = requests.get(KNOWN_ISSUES_URL, timeout=12, headers=UA, allow_redirects=True)
        if r.status_code == 200:
            return "info", "Bekannte Probleme gelistet."
        if r.status_code in (301,302,303,307,308):
            return "info", "Forum erreichbar (weitergeleitet)."
        return "unknown", "Forum nicht erreichbar."
    return with_retry(_f, fallback=("unknown", "Forum nicht erreichbar."))

# ---------- Bewertung ----------
def severity_from_latency(avg):
    if avg is None: return "unknown"
    if avg >= WARN_MS: return "warn"
    if avg >= INFO_MS: return "info"
    return "ok"

def worst_state(states):
    order = {"ok":0,"info":1,"warn":2,"unknown":3}
    return max(states, key=lambda s: order.get(s,0))

def color_for(state):
    return {"ok":0x2ECC71,"info":0x3498DB,"warn":0xF1C40F,"unknown":0x95A5A6}[state]

# ---------- Messungen ----------
def aggregate_region(hosts):
    vals = [m for h in hosts if (m := tcp_handshake_ms(h)) is not None]
    if not vals: return {"min":None,"avg":None,"max":None}
    return {"min":min(vals),"avg":round(sum(vals)/len(vals),1),"max":max(vals)}

# ---------- Verlauf & Sparkline ----------
def append_history(is_ok: bool):
    hist = read_json(HIST_FILE, [])
    hist.append({"t": int(time.time()), "ok": 1 if is_ok else 0})
    hist = hist[-168:]  # 7 Tage
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
        n = max(2,len(hist))
        step = (w-16)/(n-1)
        pts=[]
        for i,e in enumerate(hist):
            y = 10 + (1-e["ok"])*(h-20)
            x = 8 + i*step
            pts.append((x,y))
        d.line(pts, width=2)
        ok24 = sum(x["ok"] for x in hist[-24:]) / max(1,len(hist[-24:])) * 100
        ok168 = sum(x["ok"] for x in hist) / len(hist) * 100
        d.text((10,h-14), f"Uptime 24h: {ok24:.0f}% • 7T: {ok168:.0f}%")
        SPARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(SPARK_PATH)
    except Exception:
        # Grafik ist optional – Script darf nicht fehlschlagen
        pass

# ---------- Discord ----------
def parse_webhook_parts():
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    try:
        i = parts.index("webhooks")
        return parts[i+1], parts[i+2]
    except Exception as e:
        raise ValueError("Ungültige DISCORD_WEBHOOK_URL") from e

def discord_request(method, url, json_payload):
    # Rate-Limit robust
    for attempt in range(4):
        r = requests.request(method, url, json=json_payload, timeout=20, headers=UA)
        if r.status_code != 429:
            return r
        retry = float(r.headers.get("Retry-After", "1"))
        time.sleep(bounded(retry, 1, 10))
    return r  # letzter Versuch zurück

def send_new(payload):
    r = discord_request("POST", WEBHOOK + "?wait=true", payload)
    r.raise_for_status()
    return r.json()["id"]

def edit_existing(mid, payload):
    wid, tok = parse_webhook_parts()
    url = f"https://discord.com/api/webhooks/{wid}/{tok}/messages/{mid}"
    return discord_request("PATCH", url, payload)

def maybe_alert_ping(new_state, old_state):
    if not ALERT_ROLE_ID: return
    if old_state == "ok" and new_state in ("warn","unknown"):
        content = f"<@&{ALERT_ROLE_ID}> Statuswechsel: **{old_state.upper()} → {new_state.upper()}**"
        try:
            discord_request("POST", WEBHOOK, {"content":content, "allowed_mentions":{"parse":["roles"]}})
        except Exception:
            pass

# ---------- Main ----------
if __name__ == "__main__":
    # Quellen
    maint_state, maint_msg = maintenance_status()
    issues_state, issues_msg = known_issues_status()
    oauth_ok, oauth_code = http_head_ok(OAUTH_URL)
    cert_days = cert_days_left("overwatch.blizzard.com")

    # Regionen
    regions = {r: aggregate_region(h) for r,h in REGION_HOSTS.items()}

    # Trends
    last_lat = read_json(TREND_FILE, {})
    trends = {}
    for r, vals in regions.items():
        prev = last_lat.get(r, {}).get("avg")
        cur  = vals["avg"]
        if prev is None or cur is None: trends[r] = "•"
        else: trends[r] = "▲" if cur>prev+20 else ("▼" if cur<prev-20 else "→")
    write_json(TREND_FILE, regions)

    # Gesamtstatus (Hysterese light via Verlauf)
    parts = [maint_state, issues_state]
    parts += [severity_from_latency(v["avg"]) for v in regions.values()]
    if not oauth_ok: parts.append("info")
    if cert_days is not None and cert_days < CERT_WARN_DAYS: parts.append("info")
    new_state = worst_state(parts)

    # Verlauf + Sparkline
    hist = append_history(new_state == "ok")
    render_sparkline(hist)

    # Embed
    fields=[]
    for r, v in regions.items():
        if v["avg"] is None: val = "keine Messung"
        else: val = f'Ø {v["avg"]} ms ({v["min"]}/{v["max"]}) {trends[r]}'
        fields.append({"name": f"{r} – Erreichbarkeit", "value": val, "inline": True})
    fields.append({"name":"Wartung", "value": f"[{maint_msg}]({MAINT_URL})", "inline": False})
    fields.append({"name":"Known Issues", "value": f"[{issues_msg}]({KNOWN_ISSUES_URL})", "inline": False})
    if not oauth_ok:
        fields.append({"name":"Login/OAuth", "value": f"Unerwarteter Status {oauth_code}", "inline": True})
    if cert_days is not None:
        fields.append({"name":"TLS-Zertifikat", "value": f"{cert_days} Tage gültig", "inline": True})

    embed = {
        "title": "Overwatch 2 – Status",
        "description": "Heuristische Erreichbarkeit & Hinweise (kein offizieller Live-Status).",
        "color": color_for(new_state),
        "fields": fields,
        "footer": {"text": f"Letzte Prüfung: {now_utc_str()}"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    if THUMB_URL:
        embed["thumbnail"] = {"url": THUMB_URL}
    if REPO and SPARK_PATH.exists():
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{REPO}/main/assets/sparkline.png"}

    components = [{"type":1,"components":[
        {"type":2,"style":5,"label":"Maintenance","url":MAINT_URL},
        {"type":2,"style":5,"label":"Known Issues","url":KNOWN_ISSUES_URL},
        {"type":2,"style":5,"label":"Support","url":"https://support.blizzard.com"}
    ]}]

    payload = {"embeds":[embed], "components":components}

    # Diff-Only: wenn unverändert → exit
    last = read_json(LAST_FILE, None)
    if last == payload:
        raise SystemExit(0)
    write_json(LAST_FILE, payload)

    # Editieren / Erstellen
    old_state = read_json(STATE_JSON, {"state":"ok"})["state"]
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

    # Optionaler Ping bei Statuswechsel
    if old_state != new_state:
        maybe_alert_ping(new_state, old_state)
        write_json(STATE_JSON, {"state":new_state})