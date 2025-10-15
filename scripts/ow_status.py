# scripts/ow_status.py
import os, time, json, socket, datetime, re
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# =========================
# Grundkonfiguration / State-Verzeichnisse
# =========================
STATE_DIR  = Path(".bot_state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE   = STATE_DIR / "ow_message_id.txt"
LAST_FILE  = STATE_DIR / "last_payload.json"
HIST_FILE  = STATE_DIR / "history.json"
LAT_FILE   = STATE_DIR / "last_latency.json"
STATE_FILE = STATE_DIR / "state.json"
CHANGELOG  = STATE_DIR / "changelog.json"
PLATFORM_CACHE = STATE_DIR / "platform_cache.json"

SPARK_PATH = Path("assets/sparkline.png")
REPO       = os.environ.get("GITHUB_REPOSITORY", "")

WEBHOOK   = os.environ["DISCORD_WEBHOOK_URL"].strip()
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
REGIONS   = [r.strip() for r in os.environ.get("REGIONS", "EU,NA,ASIA").split(",") if r.strip()]

UA = {"User-Agent": "OW2-Status/2.0 (+github-actions)"}

# =========================
# Farbdefinitionen & Schwellen
# =========================
COLORS = {"ok": 0x2ECC71, "info": 0x3498DB, "warn": 0xF39C12, "error": 0xE74C3C, "unknown": 0x95A5A6}
ORDER  = {"ok":0, "info":1, "warn":2, "error":3, "unknown":4}
INFO_MS = float(os.environ.get("INFO_MS", "150"))
WARN_MS = float(os.environ.get("WARN_MS", "300"))

# =========================
# Overwatch Server Endpoints (offiziell)
# =========================
OW_SERVERS = {
    "EU": [
        ("185.60.112.157", 1119),  # EU OW Server
        ("185.60.114.159", 1119),
    ],
    "NA": [
        ("24.105.30.129", 1119),  # US OW Server
        ("24.105.62.129", 1119),
    ],
    "ASIA": [
        ("210.155.153.10", 1119),  # KR OW Server
        ("210.155.159.10", 1119),
    ]
}

# =========================
# Hilfsfunktionen
# =========================
def now_utc():     return datetime.datetime.now(datetime.UTC)
def now_utc_str(): return now_utc().strftime("%Y-%m-%d %H:%M UTC")

def read_json(p: Path, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

# =========================
# Overwatch Server Ping (TCP)
# =========================
def tcp_ping(host, port=1119, timeout=5.0):
    """Direkter TCP-Ping zu Overwatch Servern"""
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.time()-t0)*1000.0, 1)
    except (socket.timeout, OSError):
        return None

def check_ow_region(region_servers):
    """Prüft mehrere Server einer Region und gibt Durchschnitt zurück"""
    results = []
    for host, port in region_servers:
        ms = tcp_ping(host, port)
        if ms is not None:
            results.append(ms)
    
    if not results:
        return {"min": None, "avg": None, "max": None, "reachable": 0, "total": len(region_servers)}
    
    return {
        "min": min(results),
        "avg": round(sum(results)/len(results), 1),
        "max": max(results),
        "reachable": len(results),
        "total": len(region_servers)
    }

def severity_from_latency(data):
    """Bestimmt Status basierend auf Latenz und Erreichbarkeit"""
    if data["avg"] is None or data["reachable"] == 0:
        return "error"
    if data["reachable"] < data["total"] / 2:
        return "warn"
    if data["avg"] >= WARN_MS:
        return "warn"
    if data["avg"] >= INFO_MS:
        return "info"
    return "ok"

def worst_state(states):
    return max(states, key=lambda s: ORDER.get(s,0))

# =========================
# Wartungen & Patch Notes
# =========================
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
PATCH_NOTES_URL = "https://overwatch.blizzard.com/en-us/news/patch-notes/"
KNOWN_ISSUES_JSON = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64.json"

def fetch_maintenance():
    """Prüft Wartungsseite auf geplante Wartungen"""
    try:
        r = requests.get(MAINT_URL, timeout=12, headers=UA)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True).lower()
        
        # Suche nach Wartungsankündigungen
        if "overwatch" in text:
            if "maintenance scheduled" in text or "scheduled maintenance" in text:
                # Versuche Datum zu extrahieren
                date_pattern = r"(\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{4})"
                match = re.search(date_pattern, text, re.I)
                date_str = match.group(1) if match else "siehe Seite"
                return "warn", f"⚠️ Geplante Wartung: {date_str}", True
            elif "maintenance" in text or "downtime" in text:
                return "info", "ℹ️ Wartungshinweise vorhanden", True
        
        return "ok", "✅ Keine geplanten Wartungen", False
    except Exception as e:
        return "unknown", f"⚠️ Wartungsseite nicht erreichbar: {str(e)[:50]}", False

def fetch_patch_notes():
    """Holt die neuesten Patch Notes"""
    try:
        r = requests.get(PATCH_NOTES_URL, timeout=12, headers=UA)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Suche nach dem neuesten Patch
        article = soup.find("article") or soup.find("div", class_=re.compile("patch|news"))
        if article:
            title = article.find(["h1", "h2", "h3"])
            date = article.find("time")
            
            if title:
                patch_title = title.get_text(strip=True)
                patch_date = date.get_text(strip=True) if date else "Datum unbekannt"
                
                # Prüfe ob innerhalb der letzten 7 Tage
                is_new = False
                if date and date.get("datetime"):
                    try:
                        dt = datetime.datetime.fromisoformat(date["datetime"].replace("Z", "+00:00"))
                        is_new = (datetime.datetime.now(datetime.UTC) - dt).days <= 7
                    except:
                        pass
                
                return patch_title, patch_date, PATCH_NOTES_URL, is_new
        
        return "Keine Patch Notes gefunden", "", PATCH_NOTES_URL, False
    except Exception:
        return "Patch Notes nicht verfügbar", "", PATCH_NOTES_URL, False

def fetch_known_issues():
    """Holt bekannte Probleme aus dem Forum"""
    try:
        r = requests.get(KNOWN_ISSUES_JSON, timeout=12, headers=UA)
        r.raise_for_status()
        data = r.json()
        topics = data.get("topic_list", {}).get("topics", [])
        
        if not topics:
            return 0, [], "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"
        
        # Zähle Issues der letzten 24h
        now_ts = time.time()
        day_ago = now_ts - 24*3600
        recent_count = 0
        recent_issues = []
        
        for t in topics[:5]:  # Nur Top 5
            t_iso = t.get("last_posted_at") or t.get("created_at")
            if t_iso:
                try:
                    ts = datetime.datetime.fromisoformat(t_iso.replace("Z", "+00:00")).timestamp()
                    if ts >= day_ago:
                        recent_count += 1
                        recent_issues.append({
                            "title": t.get("title", "Unbekannt"),
                            "url": f"https://us.forums.blizzard.com/en/overwatch/t/{t.get('slug')}/{t.get('id')}"
                        })
                except:
                    pass
        
        return recent_count, recent_issues[:3], "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"
    except Exception:
        return 0, [], "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"

# =========================
# Plattform Status (verbessert)
# =========================
PLATFORM_ENDPOINTS = {
    "PC": {
        "check": [
            ("overwatch.blizzard.com", 443),
            ("eu.actual.battle.net", 443),
        ],
        "status_url": "https://downdetector.com/status/overwatch/"
    },
    "PlayStation": {
        "check": [
            ("psn-rsc.prod.dl.playstation.net", 443),
            ("account.sonyentertainmentnetwork.com", 443),
        ],
        "status_url": "https://status.playstation.com"
    },
    "Xbox": {
        "check": [
            ("xsts.auth.xboxlive.com", 443),
            ("title.mgt.xboxlive.com", 443),
        ],
        "status_url": "https://support.xbox.com/xbox-live-status"
    },
    "Switch": {
        "check": [
            ("accounts.nintendo.com", 443),
            ("atum.hac.lp1.d4c.nintendo.net", 443),
        ],
        "status_url": "https://www.nintendo.co.jp/netinfo/en_US/index.html"
    }
}

def check_platform(platform_config):
    """Prüft Plattform-Erreichbarkeit"""
    success_count = 0
    total = len(platform_config["check"])
    
    for host, port in platform_config["check"]:
        try:
            with socket.create_connection((host, port), timeout=5.0):
                success_count += 1
        except:
            pass
    
    if success_count == total:
        return "ok"
    elif success_count >= total / 2:
        return "info"
    elif success_count > 0:
        return "warn"
    else:
        return "error"

# =========================
# Verlauf & Sparkline
# =========================
def append_history(is_ok: bool):
    hist = read_json(HIST_FILE, [])
    hist.append({"t": int(time.time()), "ok": 1 if is_ok else 0})
    hist = hist[-168:]  # 7 Tage (stündlich)
    write_json(HIST_FILE, hist)
    return hist

def calculate_uptime(hist):
    if not hist: return (0, 0)
    last24 = hist[-24:] if len(hist) >= 24 else hist
    u24 = round(sum(x["ok"] for x in last24) / len(last24) * 100)
    u7 = round(sum(x["ok"] for x in hist) / len(hist) * 100)
    return (u24, u7)

def render_sparkline(hist):
    """Erstellt eine Sparkline-Grafik"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        if not hist: return
        
        w, h = 600, 100
        img = Image.new("RGB", (w, h), (47, 49, 54))  # Discord dark theme
        d = ImageDraw.Draw(img)
        
        # Rahmen
        d.rectangle([0, 0, w-1, h-1], outline=(88, 101, 242), width=2)
        
        # Datenpunkte
        n = max(2, len(hist))
        step = (w - 40) / (n - 1)
        pts = [(20 + i * step, h - 20 - (e["ok"] * (h - 40))) for i, e in enumerate(hist)]
        
        # Linie zeichnen
        if len(pts) > 1:
            d.line(pts, fill=(88, 101, 242), width=3)
        
        # Punkte
        for x, y in pts:
            d.ellipse([x-3, y-3, x+3, y+3], fill=(88, 101, 242))
        
        # Uptime Text
        u24, u7 = calculate_uptime(hist)
        d.text((10, h-15), f"Uptime: 24h {u24}% • 7d {u7}%", fill=(220, 221, 222))
        
        SPARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(SPARK_PATH)
    except Exception as e:
        print(f"Sparkline-Fehler: {e}")

# =========================
# Discord Webhook
# =========================
def parse_webhook():
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    i = parts.index("webhooks")
    return parts[i+1], parts[i+2]

def discord_request(method, url, json_payload):
    for attempt in range(4):
        r = requests.request(method, url, json=json_payload, timeout=20, headers=UA)
        if r.status_code != 429:
            return r
        retry_after = float(r.headers.get("Retry-After", "1"))
        time.sleep(min(max(retry_after, 1), 10))
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
# MAIN
# =========================
if __name__ == "__main__":
    print("🔍 Starte Overwatch 2 Status Check...")
    
    # 1. Regionale Server prüfen
    print("📡 Prüfe regionale Server...")
    regions = {}
    for region in REGIONS:
        if region in OW_SERVERS:
            regions[region] = check_ow_region(OW_SERVERS[region])
            print(f"  {region}: {regions[region]['avg']}ms ({regions[region]['reachable']}/{regions[region]['total']} Server)")
    
    # Trends berechnen
    last_lat = read_json(LAT_FILE, {})
    trends = {}
    for r in REGIONS:
        prev = last_lat.get(r, {}).get("avg")
        cur = regions[r]["avg"]
        if prev is None or cur is None:
            trends[r] = "•"
        else:
            trends[r] = "📈" if cur > (prev + 30) else ("📉" if cur < (prev - 30) else "➡️")
    write_json(LAT_FILE, regions)
    
    # 2. Plattformen prüfen
    print("🎮 Prüfe Plattformen...")
    platform_states = {}
    for platform, config in PLATFORM_ENDPOINTS.items():
        state = check_platform(config)
        platform_states[platform] = state
        print(f"  {platform}: {state.upper()}")
    
    # 3. Wartungen prüfen
    print("🔧 Prüfe Wartungen...")
    maint_state, maint_msg, has_maint = fetch_maintenance()
    
    # 4. Patch Notes holen
    print("📝 Hole Patch Notes...")
    patch_title, patch_date, patch_url, is_new_patch = fetch_patch_notes()
    
    # 5. Known Issues
    print("⚠️  Hole Known Issues...")
    ki_count, ki_issues, ki_url = fetch_known_issues()
    
    # 6. Gesamtstatus berechnen
    region_states = [severity_from_latency(regions[r]) for r in REGIONS]
    all_states = region_states + list(platform_states.values()) + [maint_state]
    overall_state = worst_state(all_states)
    
    # 7. Historie aktualisieren
    hist = append_history(overall_state in ["ok", "info"])
    u24, u7 = calculate_uptime(hist)
    render_sparkline(hist)
    
    # 8. Changelog
    old_state = read_json(STATE_FILE, {"state": "ok"})["state"]
    if old_state != overall_state:
        print(f"📊 Status geändert: {old_state} → {overall_state}")
        write_json(STATE_FILE, {"state": overall_state})
    
    # =========================
    # Discord Embed erstellen
    # =========================
    
    # Status Emoji
    status_emoji = {
        "ok": "🟢",
        "info": "🟡", 
        "warn": "🟠",
        "error": "🔴",
        "unknown": "⚪"
    }
    
    # Titel mit Status
    title = f"{status_emoji.get(overall_state, '⚪')} Overwatch 2 Server Status"
    
    # Beschreibung
    desc_parts = []
    for region in REGIONS:
        data = regions[region]
        if data["avg"]:
            status = status_emoji.get(severity_from_latency(data), "⚪")
            desc_parts.append(f"{status} **{region}**: {data['avg']}ms {trends[region]}")
        else:
            desc_parts.append(f"🔴 **{region}**: Nicht erreichbar")
    
    desc_parts.append(f"\n📊 **Uptime**: 24h: {u24}% • 7d: {u7}%")
    description = "\n".join(desc_parts)
    
    # Felder
    fields = []
    
    # Regionen Details
    for region in REGIONS:
        data = regions[region]
        if data["avg"]:
            value = f"```\n📍 Latenz: {data['avg']}ms\n⚡ Min/Max: {data['min']}/{data['max']}ms\n🌐 Server: {data['reachable']}/{data['total']} erreichbar\n```"
        else:
            value = "```\n🔴 Keine Verbindung möglich\n```"
        fields.append({"name": f"🌍 {region} Region", "value": value, "inline": True})
    
    # Plattformen
    plat_lines = []
    for plat in ["PC", "PlayStation", "Xbox", "Switch"]:
        state = platform_states.get(plat, "unknown")
        emoji = status_emoji.get(state, "⚪")
        plat_lines.append(f"{emoji} **{plat}**: {state.upper()}")
    fields.append({
        "name": "🎮 Plattformen",
        "value": "\n".join(plat_lines),
        "inline": False
    })
    
    # Wartungen
    if has_maint:
        fields.append({
            "name": "🔧 Wartungen",
            "value": f"{maint_msg}\n[Mehr Infos]({MAINT_URL})",
            "inline": False
        })
    
    # Patch Notes (nur wenn neu)
    if is_new_patch:
        fields.append({
            "name": "📝 Neuester Patch",
            "value": f"**{patch_title}**\n{patch_date}\n[Patch Notes lesen]({patch_url})",
            "inline": False
        })
    
    # Known Issues
    if ki_count > 0:
        ki_text = f"**{ki_count} neue/aktualisierte Issues (24h)**\n"
        for issue in ki_issues:
            ki_text += f"• [{issue['title'][:60]}...]({issue['url']})\n"
        ki_text += f"[Alle Issues ansehen]({ki_url})"
        fields.append({
            "name": "⚠️ Bekannte Probleme",
            "value": ki_text,
            "inline": False
        })
    
    # Embed zusammenbauen
    embed = {
        "title": title,
        "description": description,
        "color": COLORS.get(overall_state, COLORS["unknown"]),
        "fields": fields,
        "footer": {"text": f"Letzte Prüfung: {now_utc_str()} • Status: {overall_state.upper()}"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    
    if THUMB_URL:
        embed["thumbnail"] = {"url": THUMB_URL}
    
    if REPO and SPARK_PATH.exists():
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{REPO}/main/assets/sparkline.png?t={int(time.time())}"}
    
    # Buttons
    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 5, "label": "🔧 Wartungen", "url": MAINT_URL},
            {"type": 2, "style": 5, "label": "📝 Patch Notes", "url": patch_url},
            {"type": 2, "style": 5, "label": "⚠️ Known Issues", "url": ki_url},
            {"type": 2, "style": 5, "label": "💬 Support", "url": "https://support.blizzard.com"}
        ]
    }]
    
    payload = {"embeds": [embed], "components": components}
    
    # Nur senden wenn sich etwas geändert hat
    last = read_json(LAST_FILE, None)
    if last == payload:
        print("✅ Keine Änderungen, überspringe Update")
        raise SystemExit(0)
    
    write_json(LAST_FILE, payload)
    
    # An Discord senden
    print("📤 Sende Update an Discord...")
    mid = MID_FILE.read_text().strip() if MID_FILE.exists() else None
    
    if mid:
        r = edit_existing(mid, payload)
        if r.status_code == 404:
            print("⚠️  Nachricht nicht gefunden, erstelle neue...")
            mid = None
        else:
            r.raise_for_status()
            print("✅ Nachricht aktualisiert")
    
    if not mid:
        new_id = send_new(payload)
        MID_FILE.write_text(str(new_id))
        print(f"✅ Neue Nachricht erstellt (ID: {new_id})")
    
    print("✅ Status-Check abgeschlossen!")