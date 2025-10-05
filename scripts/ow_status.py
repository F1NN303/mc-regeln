import os, time, json, ssl, socket, datetime, requests, statistics
from pathlib import Path
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

# === Konfiguration ===
WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
ALERT_ROLE_ID = os.environ.get("ALERT_ROLE_ID", "").strip()
REGION_ROLE_IDS = json.loads(os.environ.get("REGION_ROLE_IDS", "{}"))  # {"EU": "123", "NA": "456"}

REGION_HOSTS = {
    "EU":   ["eu.actual.battle.net", "overwatch.blizzard.com"],
    "NA":   ["us.actual.battle.net", "overwatch.blizzard.com"],
    "ASIA": ["kr.actual.battle.net", "overwatch.blizzard.com"],
}
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
KNOWN_ISSUES_URL = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"
OAUTH_URL = "https://oauth.battle.net/authorize?response_type=code&client_id=dummy&redirect_uri=https://example.com"
BLIZZARD_CS_TWITTER = "https://x.com/BlizzardCS"

# Schwellwerte
INFO_MS = float(os.environ.get("INFO_MS", "200"))
WARN_MS = float(os.environ.get("WARN_MS", "400"))
CRITICAL_MS = float(os.environ.get("CRITICAL_MS", "800"))
CERT_WARN_DAYS = int(os.environ.get("CERT_WARN_DAYS", "14"))
JITTER_WARN_MS = float(os.environ.get("JITTER_WARN_MS", "100"))
ALERT_COOLDOWN_HOURS = int(os.environ.get("ALERT_COOLDOWN_HOURS", "2"))
ESCALATION_MINUTES = int(os.environ.get("ESCALATION_MINUTES", "30"))

STATE_DIR = Path(".bot_state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE   = STATE_DIR / "ow_message_id.txt"
HIST_FILE  = STATE_DIR / "history.json"
LAST_FILE  = STATE_DIR / "last_payload.json"
TREND_FILE = STATE_DIR / "last_latency.json"
ALERT_FILE = STATE_DIR / "last_alert.json"
INCIDENT_FILE = STATE_DIR / "incidents.json"
SPARK_PATH = Path("assets/sparkline.png")
HEATMAP_PATH = Path("assets/heatmap.png")

def _now_utc_str(): return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')
def _now_ts(): return int(time.time())

# === Erweiterte Messungen ===
def tcp_handshake_ms(host, port=443, timeout=3.0):
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.time()-t0)*1000.0, 1)
    except OSError:
        return None

def measure_jitter(host, port=443, samples=5, timeout=3.0):
    """Misst Latenz-Varianz über mehrere Samples"""
    measurements = []
    for _ in range(samples):
        ms = tcp_handshake_ms(host, port, timeout)
        if ms is not None:
            measurements.append(ms)
        time.sleep(0.2)
    
    if len(measurements) < 2:
        return None, None
    
    avg = statistics.mean(measurements)
    jitter = statistics.stdev(measurements)
    return round(avg, 1), round(jitter, 1)

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

def aggregate_region_parallel(region, hosts):
    """Parallele Messung mit Jitter-Detection"""
    results = []
    
    with ThreadPoolExecutor(max_workers=len(hosts)) as executor:
        future_to_host = {executor.submit(measure_jitter, h): h for h in hosts}
        
        for future in as_completed(future_to_host):
            host = future_to_host[future]
            try:
                avg, jitter = future.result()
                if avg is not None:
                    results.append({"host": host, "avg": avg, "jitter": jitter})
            except Exception:
                pass
    
    if not results:
        return {"min": None, "avg": None, "max": None, "jitter": None}
    
    avgs = [r["avg"] for r in results]
    jitters = [r["jitter"] for r in results if r["jitter"] is not None]
    
    return {
        "min": round(min(avgs), 1),
        "avg": round(statistics.mean(avgs), 1),
        "max": round(max(avgs), 1),
        "jitter": round(statistics.mean(jitters), 1) if jitters else None
    }

# === Status-Quellen ===
def maintenance_status():
    try:
        html = requests.get(MAINT_URL, timeout=20).text
        soup = BeautifulSoup(html, "html.parser")
        txt = soup.get_text(" ").lower()
        
        # Suche nach Zeitangaben
        if "overwatch" in txt and any(k in txt for k in ["maintenance", "downtime", "scheduled"]):
            # Versuche Zeitfenster zu extrahieren
            return "warn", "Wartungshinweise gefunden (siehe Link)"
        return "ok", "Keine geplanten Wartungen"
    except Exception as e:
        return "unknown", f"Wartungsseite nicht erreichbar"

def known_issues_status():
    try:
        r = requests.get(KNOWN_ISSUES_URL, timeout=12)
        if r.status_code == 200:
            return "info", "Bekannte Probleme gelistet"
        return "ok", "Forum erreichbar"
    except Exception:
        return "unknown", "Forum nicht erreichbar"

def check_blizzard_twitter():
    """Prüft auf aktuelle Statusmeldungen (vereinfacht, da keine API)"""
    try:
        r = requests.get(BLIZZARD_CS_TWITTER, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if r.status_code == 200:
            return "ok", "Twitter erreichbar"
        return "unknown", "Twitter nicht prüfbar"
    except Exception:
        return "unknown", "Twitter nicht prüfbar"

# === Bewertung ===
def severity_from_metrics(avg, jitter):
    """Erweiterte Severity mit Jitter"""
    if avg is None:
        return "unknown"
    
    # Kritisch bei sehr hoher Latenz
    if avg >= CRITICAL_MS:
        return "critical"
    
    # Warnung bei hoher Latenz ODER hohem Jitter
    if avg >= WARN_MS or (jitter and jitter >= JITTER_WARN_MS):
        return "warn"
    
    if avg >= INFO_MS:
        return "info"
    
    return "ok"

def worst_state(states):
    order = {"ok": 0, "info": 1, "warn": 2, "critical": 3, "unknown": 3}
    return max(states, key=lambda s: order.get(s, 3))

def color_for(state):
    return {
        "ok": 0x2ECC71,
        "info": 0x3498DB,
        "warn": 0xF1C40F,
        "critical": 0xE74C3C,
        "unknown": 0x95A5A6
    }[state]

def emoji_for(state):
    return {
        "ok": "✅",
        "info": "ℹ️",
        "warn": "⚠️",
        "critical": "🔴",
        "unknown": "❓"
    }[state]

# === Persistenz ===
def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2))

# === Verlauf & Visualisierung ===
def append_history(state, regions):
    """Speichert detaillierten History-Entry"""
    hist = read_json(HIST_FILE, [])
    entry = {
        "t": _now_ts(),
        "state": state,
        "ok": 1 if state == "ok" else 0,
        "regions": {r: v["avg"] for r, v in regions.items()}
    }
    hist.append(entry)
    hist = hist[-168:]  # 7 Tage
    write_json(HIST_FILE, hist)
    return hist

def render_sparkline(hist):
    """Erweiterte Sparkline mit Farbcodierung"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        if not hist:
            return
        
        w, h = 500, 80
        img = Image.new("RGB", (w, h), (24, 26, 27))
        d = ImageDraw.Draw(img)
        
        # Border
        d.rectangle([0, 0, w-1, h-1], outline=(60, 60, 60))
        
        # Datenpunkte
        n = max(2, len(hist))
        step = (w - 20) / (n - 1)
        
        for i, e in enumerate(hist):
            x = 10 + i * step
            
            # Höhe basierend auf Status
            if e["state"] == "ok":
                y = h - 25
                col = (46, 204, 113)
            elif e["state"] == "info":
                y = h - 35
                col = (52, 152, 219)
            elif e["state"] == "warn":
                y = h - 45
                col = (241, 196, 15)
            else:  # critical/unknown
                y = h - 55
                col = (231, 76, 60)
            
            d.ellipse([x-2, y-2, x+2, y+2], fill=col)
            
            if i > 0:
                prev_e = hist[i-1]
                prev_y = {
                    "ok": h - 25,
                    "info": h - 35,
                    "warn": h - 45
                }.get(prev_e["state"], h - 55)
                prev_x = 10 + (i-1) * step
                d.line([prev_x, prev_y, x, y], fill=(100, 120, 140), width=1)
        
        # Statistiken
        ok24 = sum(x["ok"] for x in hist[-24:]) / max(1, len(hist[-24:])) * 100
        ok168 = sum(x["ok"] for x in hist) / len(hist) * 100
        
        d.text((10, h-16), f"Uptime: 24h={ok24:.0f}% • 7d={ok168:.0f}%", fill=(180, 180, 180))
        
        SPARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(SPARK_PATH)
    except Exception as e:
        print(f"Sparkline-Fehler: {e}")

def render_heatmap(hist):
    """Wochentags-/Stunden-Heatmap"""
    try:
        from PIL import Image, ImageDraw
        if len(hist) < 24:
            return
        
        w, h = 420, 160
        img = Image.new("RGB", (w, h), (24, 26, 27))
        d = ImageDraw.Draw(img)
        
        # 7 Tage × 24 Stunden Grid
        cell_w, cell_h = w // 24, h // 7
        
        # Aggregiere nach Wochentag/Stunde
        grid = [[0 for _ in range(24)] for _ in range(7)]
        counts = [[0 for _ in range(24)] for _ in range(7)]
        
        for entry in hist:
            dt = datetime.datetime.fromtimestamp(entry["t"])
            wd = dt.weekday()
            hr = dt.hour
            grid[wd][hr] += entry["ok"]
            counts[wd][hr] += 1
        
        # Zeichne Heatmap
        for wd in range(7):
            for hr in range(24):
                if counts[wd][hr] == 0:
                    continue
                    
                ratio = grid[wd][hr] / counts[wd][hr]
                
                # Farbe: Rot (schlecht) bis Grün (gut)
                if ratio > 0.9:
                    col = (46, 204, 113)
                elif ratio > 0.7:
                    col = (241, 196, 15)
                else:
                    col = (231, 76, 60)
                
                x1 = hr * cell_w
                y1 = wd * cell_h
                d.rectangle([x1, y1, x1+cell_w-1, y1+cell_h-1], fill=col, outline=(40, 40, 40))
        
        HEATMAP_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(HEATMAP_PATH)
    except Exception as e:
        print(f"Heatmap-Fehler: {e}")

# === Incident-Tracking ===
def check_escalation(regions):
    """Prüft auf anhaltende Probleme"""
    incidents = read_json(INCIDENT_FILE, {})
    now = _now_ts()
    escalations = []
    
    for region, metrics in regions.items():
        state = severity_from_metrics(metrics["avg"], metrics["jitter"])
        
        if state in ("warn", "critical"):
            if region not in incidents:
                incidents[region] = {"start": now, "state": state}
            else:
                duration_min = (now - incidents[region]["start"]) / 60
                if duration_min >= ESCALATION_MINUTES:
                    escalations.append((region, state, duration_min))
        else:
            if region in incidents:
                del incidents[region]
    
    write_json(INCIDENT_FILE, incidents)
    return escalations

# === Discord Integration ===
def send_new(payload):
    r = requests.post(WEBHOOK + "?wait=true", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["id"]

def edit_existing(mid, payload):
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    i = parts.index("webhooks")
    wid, tok = parts[i+1], parts[i+2]
    url = f"https://discord.com/api/webhooks/{wid}/{tok}/messages/{mid}"
    return requests.patch(url, json=payload, timeout=20)

def should_alert(new_state, old_state):
    """Prüft Alert-Cooldown"""
    if not ALERT_ROLE_ID:
        return False
    
    if old_state == "ok" and new_state in ("warn", "critical", "unknown"):
        last_alert = read_json(ALERT_FILE, {"timestamp": 0})
        if (_now_ts() - last_alert["timestamp"]) / 3600 >= ALERT_COOLDOWN_HOURS:
            return True
    
    return False

def send_alert(new_state, old_state, escalations=None):
    """Sendet Alert-Nachricht"""
    content_parts = [f"<@&{ALERT_ROLE_ID}> **Statuswechsel: {old_state.upper()} → {new_state.upper()}**"]
    
    if escalations:
        content_parts.append("\n⚠️ **Anhaltende Probleme:**")
        for region, state, duration in escalations:
            content_parts.append(f"• {region}: {state.upper()} seit {duration:.0f} min")
            
            # Region-spezifischer Ping
            if region in REGION_ROLE_IDS:
                content_parts.append(f"  <@&{REGION_ROLE_IDS[region]}>")
    
    content = "\n".join(content_parts)
    
    try:
        requests.post(WEBHOOK, json={
            "content": content,
            "allowed_mentions": {"parse": ["roles"]}
        }, timeout=10)
        write_json(ALERT_FILE, {"timestamp": _now_ts(), "state": new_state})
    except Exception as e:
        print(f"Alert-Fehler: {e}")

def create_incident_thread(region, state):
    """Erstellt Thread für Incident (wenn Message-ID bekannt)"""
    # Benötigt Discord API Token, hier vereinfacht
    pass

# === Main ===
def main():
    print(f"[{_now_utc_str()}] Starte Statusprüfung...")
    
    # Externe Checks
    maint_state, maint_msg = maintenance_status()
    issues_state, issues_msg = known_issues_status()
    twitter_state, twitter_msg = check_blizzard_twitter()
    http_ok, http_code = http_check(OAUTH_URL)
    cert_days = cert_days_left("overwatch.blizzard.com")
    
    # Region-Messungen (parallel)
    regions = {}
    with ThreadPoolExecutor(max_workers=len(REGION_HOSTS)) as executor:
        future_to_region = {
            executor.submit(aggregate_region_parallel, r, hosts): r 
            for r, hosts in REGION_HOSTS.items()
        }
        
        for future in as_completed(future_to_region):
            region = future_to_region[future]
            regions[region] = future.result()
    
    # Trendvergleich
    last_lat = read_json(TREND_FILE, {})
    trends = {}
    for r, vals in regions.items():
        prev = last_lat.get(r, {}).get("avg")
        cur = vals["avg"]
        if prev is None or cur is None:
            trends[r] = "•"
        else:
            diff = cur - prev
            if diff > 50: trends[r] = "📈"
            elif diff > 20: trends[r] = "▲"
            elif diff < -50: trends[r] = "📉"
            elif diff < -20: trends[r] = "▼"
            else: trends[r] = "→"
    
    write_json(TREND_FILE, regions)
    
    # State-Bewertung
    states = [maint_state, issues_state]
    for r, vals in regions.items():
        states.append(severity_from_metrics(vals["avg"], vals["jitter"]))
    
    if not http_ok:
        states.append("info")
    if cert_days is not None and cert_days < CERT_WARN_DAYS:
        states.append("warn")
    
    new_state = worst_state(states)
    
    # Eskalations-Check
    escalations = check_escalation(regions)
    
    # Verlauf & Visualisierung
    hist = append_history(new_state, regions)
    render_sparkline(hist)
    render_heatmap(hist)
    
    # Embed aufbauen
    fields = []
    
    for r, vals in regions.items():
        if vals["avg"] is None:
            value = "❌ Keine Messung"
        else:
            emoji = emoji_for(severity_from_metrics(vals["avg"], vals["jitter"]))
            value = f'{emoji} **{vals["avg"]}ms** (min: {vals["min"]}ms, max: {vals["max"]}ms)'
            if vals["jitter"]:
                jitter_emoji = "⚠️" if vals["jitter"] >= JITTER_WARN_MS else "✓"
                value += f'\nJitter: {jitter_emoji} {vals["jitter"]}ms {trends[r]}'
            else:
                value += f' {trends[r]}'
        
        fields.append({"name": f"🌍 {r}", "value": value, "inline": True})
    
    # Zusatzinfos
    fields.append({"name": "🔧 Wartung", "value": f"{emoji_for(maint_state)} [{maint_msg}]({MAINT_URL})", "inline": False})
    fields.append({"name": "🐛 Known Issues", "value": f"{emoji_for(issues_state)} [{issues_msg}]({KNOWN_ISSUES_URL})", "inline": False})
    
    if not http_ok:
        fields.append({"name": "🔑 Login/OAuth", "value": f"⚠️ Status {http_code}", "inline": True})
    
    if cert_days is not None:
        cert_emoji = "⚠️" if cert_days < CERT_WARN_DAYS else "✅"
        fields.append({"name": "🔒 TLS-Zertifikat", "value": f"{cert_emoji} {cert_days} Tage gültig", "inline": True})
    
    # Eskalations-Warnung
    if escalations:
        esc_text = "\n".join([f"• **{r}**: {s.upper()} seit {d:.0f}min" for r, s, d in escalations])
        fields.append({"name": "⏰ Anhaltende Probleme", "value": esc_text, "inline": False})
    
    # Embed
    embed = {
        "title": f"{emoji_for(new_state)} Overwatch 2 – Status",
        "description": "Automatisierte Erreichbarkeitsprüfung mit Latenz-, Jitter- und Trendanalyse.",
        "color": color_for(new_state),
        "fields": fields,
        "footer": {"text": f"Letzte Prüfung: {_now_utc_str()} • Schwellwerte: Info>{INFO_MS}ms, Warn>{WARN_MS}ms"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    
    if THUMB_URL:
        embed["thumbnail"] = {"url": THUMB_URL}
    
    # Sparkline & Heatmap
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if repo and SPARK_PATH.exists():
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{repo}/main/assets/sparkline.png?t={_now_ts()}"}
    
    # Buttons
    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 5, "label": "🔧 Wartung", "url": MAINT_URL},
            {"type": 2, "style": 5, "label": "🐛 Known Issues", "url": KNOWN_ISSUES_URL},
            {"type": 2, "style": 5, "label": "💬 Support", "url": "https://support.blizzard.com"},
            {"type": 2, "style": 5, "label": "🐦 BlizzardCS", "url": BLIZZARD_CS_TWITTER}
        ]
    }]
    
    payload = {"embeds": [embed], "components": components}
    
    # Diff-Only
    last = read_json(LAST_FILE, None)
    if last == payload:
        print("Keine Änderungen, überspringe Update.")
        raise SystemExit(0)
    
    write_json(LAST_FILE, payload)
    
    # Discord Update
    old_state = read_json(STATE_DIR / "state.json", {"state": "ok"})["state"]
    mid = MID_FILE.read_text().strip() if MID_FILE.exists() else None
    
    if mid:
        r = edit_existing(mid, payload)
        if r.status_code == 404:
            mid = None
        else:
            r.raise_for_status()
            print(f"Message {mid} aktualisiert.")
    
    if not mid:
        new_id = send_new(payload)
        MID_FILE.write_text(str(new_id))
        print(f"Neue Message erstellt: {new_id}")
    
    # Alert bei Statuswechsel
    if should_alert(new_state, old_state):
        send_alert(new_state, old_state, escalations)
        print(f"Alert gesendet: {old_state} → {new_state}")
    
    write_json(STATE_DIR / "state.json", {"state": new_state})
    print(f"Status: {new_state.upper()}")