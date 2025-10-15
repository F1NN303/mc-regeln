# scripts/ow_status.py
import os, time, json, socket, datetime, re
from pathlib import Path
import requests
from bs4 import BeautifulSoup

# =========================
# Grundkonfiguration
# =========================
STATE_DIR  = Path(".bot_state"); STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE   = STATE_DIR / "ow_message_id.txt"
LAST_FILE  = STATE_DIR / "last_payload.json"
HIST_FILE  = STATE_DIR / "history.json"
STATE_FILE = STATE_DIR / "state.json"
SPARK_PATH = Path("assets/sparkline.png")
REPO       = os.environ.get("GITHUB_REPOSITORY", "")

WEBHOOK   = os.environ["DISCORD_WEBHOOK_URL"].strip()
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
REGIONS   = [r.strip() for r in os.environ.get("REGIONS", "EU,NA,ASIA").split(",") if r.strip()]

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# =========================
# Farben & Status
# =========================
COLORS = {"ok": 0x2ECC71, "info": 0x3498DB, "warn": 0xF39C12, "error": 0xE74C3C, "unknown": 0x95A5A6}
ORDER  = {"ok":0, "info":1, "warn":2, "error":3, "unknown":4}

# =========================
# Hilfsfunktionen
# =========================
def now_utc(): return datetime.datetime.now(datetime.UTC)
def now_utc_str(): return now_utc().strftime("%Y-%m-%d %H:%M UTC")

def read_json(p: Path, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except: return default

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")

def worst_state(states):
    return max(states, key=lambda s: ORDER.get(s,0))

# =========================
# Battle.net & Overwatch Server Check
# =========================
BATTLENET_ENDPOINTS = {
    "EU": [
        "https://eu.actual.battle.net:1119",
        "https://eu.launcher.battle.net",
    ],
    "NA": [
        "https://us.actual.battle.net:1119", 
        "https://us.launcher.battle.net",
    ],
    "ASIA": [
        "https://kr.actual.battle.net:1119",
        "https://kr.launcher.battle.net",
    ]
}

def check_http_endpoint(url, timeout=8):
    """Prüft HTTP/HTTPS Endpoint und misst Latenz"""
    t0 = time.time()
    try:
        # Versuche HEAD Request (schneller)
        r = requests.head(url, timeout=timeout, headers=UA, allow_redirects=True, verify=False)
        latency = round((time.time() - t0) * 1000, 1)
        # 200-399 sind OK, auch 403 ist OK (Server antwortet)
        if r.status_code < 500:
            return latency
        return None
    except requests.exceptions.SSLError:
        # SSL Error bedeutet Server ist da, aber SSL Problem
        return round((time.time() - t0) * 1000, 1)
    except:
        return None

def check_region_status(endpoints):
    """Prüft eine Region über mehrere Endpoints"""
    results = []
    for url in endpoints:
        latency = check_http_endpoint(url)
        if latency:
            results.append(latency)
    
    if not results:
        return {
            "status": "error",
            "avg": None,
            "min": None, 
            "max": None,
            "reachable": 0,
            "total": len(endpoints)
        }
    
    avg = round(sum(results) / len(results), 1)
    
    # Status bestimmen
    if len(results) == len(endpoints):
        if avg < 150:
            status = "ok"
        elif avg < 300:
            status = "info"
        else:
            status = "warn"
    elif len(results) >= len(endpoints) / 2:
        status = "warn"
    else:
        status = "error"
    
    return {
        "status": status,
        "avg": avg,
        "min": min(results),
        "max": max(results),
        "reachable": len(results),
        "total": len(endpoints)
    }

# =========================
# Downdetector Scraping (Fallback)
# =========================
def check_downdetector():
    """Scraped Downdetector für User-Reports"""
    try:
        r = requests.get("https://downdetector.com/status/overwatch/", 
                        timeout=10, headers=UA)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Suche nach "baseline" oder "spike" Status
        text = soup.get_text().lower()
        
        if "user reports indicate possible problems" in text or "reports" in text:
            # Versuche Anzahl zu extrahieren
            numbers = re.findall(r'(\d+)\s*reports?', text)
            if numbers:
                report_count = int(numbers[0])
                if report_count > 100:
                    return "error", report_count
                elif report_count > 20:
                    return "warn", report_count
                else:
                    return "info", report_count
        
        return "ok", 0
    except:
        return "unknown", None

# =========================
# Plattform-Status
# =========================
PLATFORM_CHECKS = {
    "PC": [
        "https://overwatch.blizzard.com",
        "https://eu.battle.net",
    ],
    "PlayStation": [
        "https://status.playstation.com",
    ],
    "Xbox": [
        "https://support.xbox.com/xbox-live-status",
    ],
    "Switch": [
        "https://www.nintendo.co.jp/netinfo/en_US/index.html",
    ]
}

def check_platform(name, urls):
    """Prüft Plattform Status"""
    working = 0
    for url in urls:
        try:
            r = requests.head(url, timeout=8, headers=UA, allow_redirects=True)
            if r.status_code < 500:
                working += 1
        except:
            pass
    
    if working == len(urls):
        return "ok"
    elif working > 0:
        return "info"
    else:
        return "warn"

# =========================
# Wartung & Updates
# =========================
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
PATCH_URL = "https://overwatch.blizzard.com/en-us/news/patch-notes/"
FORUM_URL = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64.json"

def check_maintenance():
    """Prüft Wartungsseite"""
    try:
        r = requests.get(MAINT_URL, timeout=10, headers=UA)
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(" ", strip=True).lower()
        
        if "overwatch" in text and ("maintenance" in text or "downtime" in text):
            # Extrahiere Datum
            date_match = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", text, re.I)
            date_str = date_match.group(1) if date_match else "siehe Seite"
            return "warn", f"⚠️ Wartung geplant: {date_str}", True
        
        return "ok", "✅ Keine Wartungen geplant", False
    except:
        return "unknown", "⚠️ Wartungsinfo nicht verfügbar", False

def fetch_latest_patch():
    """Holt neueste Patch Info"""
    try:
        r = requests.get(PATCH_URL, timeout=10, headers=UA)
        soup = BeautifulSoup(r.text, "html.parser")
        
        # Finde ersten Patch Eintrag
        article = soup.find("article") or soup.find(class_=re.compile("patch|post"))
        if article:
            title_elem = article.find(["h1", "h2", "h3"])
            if title_elem:
                title = title_elem.get_text(strip=True)
                # Prüfe auf Datum
                time_elem = article.find("time")
                if time_elem:
                    date_text = time_elem.get_text(strip=True)
                    return title, date_text, True
                return title, "Datum unbekannt", False
        
        return None, None, False
    except:
        return None, None, False

def fetch_known_issues():
    """Holt bekannte Issues"""
    try:
        r = requests.get(FORUM_URL, timeout=10, headers=UA)
        data = r.json()
        topics = data.get("topic_list", {}).get("topics", [])
        
        recent = []
        now = time.time()
        day_ago = now - 86400
        
        for topic in topics[:5]:
            ts_str = topic.get("last_posted_at") or topic.get("created_at")
            if ts_str:
                try:
                    ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    if ts >= day_ago:
                        recent.append({
                            "title": topic.get("title", "Unbekannt"),
                            "slug": topic.get("slug", ""),
                            "id": topic.get("id", "")
                        })
                except:
                    pass
        
        return len(recent), recent[:3]
    except:
        return 0, []

# =========================
# Historie & Sparkline
# =========================
def update_history(is_ok):
    hist = read_json(HIST_FILE, [])
    hist.append({"t": int(time.time()), "ok": 1 if is_ok else 0})
    hist = hist[-168:]
    write_json(HIST_FILE, hist)
    return hist

def calc_uptime(hist):
    if not hist: return (0, 0)
    last24 = hist[-24:] if len(hist) >= 24 else hist
    u24 = round(sum(x["ok"] for x in last24) / len(last24) * 100)
    u7 = round(sum(x["ok"] for x in hist) / len(hist) * 100)
    return (u24, u7)

def render_sparkline(hist):
    try:
        from PIL import Image, ImageDraw
        if not hist: return
        
        w, h = 600, 100
        img = Image.new("RGB", (w, h), (47, 49, 54))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, w-1, h-1], outline=(88, 101, 242), width=2)
        
        n = max(2, len(hist))
        step = (w - 40) / (n - 1)
        pts = [(20 + i * step, h - 20 - (e["ok"] * (h - 40))) for i, e in enumerate(hist)]
        
        if len(pts) > 1:
            d.line(pts, fill=(88, 101, 242), width=3)
        
        for x, y in pts:
            d.ellipse([x-3, y-3, x+3, y+3], fill=(88, 101, 242))
        
        u24, u7 = calc_uptime(hist)
        d.text((10, h-15), f"Uptime: 24h {u24}% • 7d {u7}%", fill=(220, 221, 222))
        
        SPARK_PATH.parent.mkdir(parents=True, exist_ok=True)
        img.save(SPARK_PATH)
    except Exception as e:
        print(f"Sparkline error: {e}")

# =========================
# Discord
# =========================
def parse_webhook():
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    i = parts.index("webhooks")
    return parts[i+1], parts[i+2]

def discord_request(method, url, payload):
    for _ in range(3):
        r = requests.request(method, url, json=payload, timeout=20, headers=UA)
        if r.status_code != 429:
            return r
        time.sleep(float(r.headers.get("Retry-After", "2")))
    return r

def send_message(payload):
    r = discord_request("POST", WEBHOOK + "?wait=true", payload)
    r.raise_for_status()
    return r.json()["id"]

def edit_message(mid, payload):
    wid, tok = parse_webhook()
    url = f"https://discord.com/api/webhooks/{wid}/{tok}/messages/{mid}"
    return discord_request("PATCH", url, payload)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("🔍 Overwatch 2 Status Check gestartet...")
    
    # 1. Regionen prüfen (Battle.net)
    print("\n📡 Prüfe Battle.net Server...")
    regions = {}
    for region in REGIONS:
        if region in BATTLENET_ENDPOINTS:
            data = check_region_status(BATTLENET_ENDPOINTS[region])
            regions[region] = data
            print(f"  {region}: {data['status'].upper()} - {data['avg']}ms ({data['reachable']}/{data['total']})")
    
    # 2. Downdetector als Zusatz-Check
    print("\n🌐 Prüfe Downdetector...")
    dd_status, dd_reports = check_downdetector()
    print(f"  Status: {dd_status.upper()}, Reports: {dd_reports}")
    
    # 3. Plattformen
    print("\n🎮 Prüfe Plattformen...")
    platforms = {}
    for name, urls in PLATFORM_CHECKS.items():
        status = check_platform(name, urls)
        platforms[name] = status
        print(f"  {name}: {status.upper()}")
    
    # 4. Wartung
    print("\n🔧 Prüfe Wartungen...")
    maint_status, maint_msg, has_maint = check_maintenance()
    print(f"  {maint_msg}")
    
    # 5. Patch Notes
    print("\n📝 Hole Patch Notes...")
    patch_title, patch_date, has_patch = fetch_latest_patch()
    if patch_title:
        print(f"  {patch_title} ({patch_date})")
    
    # 6. Known Issues
    print("\n⚠️  Hole Known Issues...")
    ki_count, ki_list = fetch_known_issues()
    print(f"  {ki_count} neue Issues in 24h")
    
    # 7. Gesamtstatus
    all_statuses = [r["status"] for r in regions.values()] + list(platforms.values()) + [dd_status, maint_status]
    overall = worst_state(all_statuses)
    print(f"\n📊 Gesamtstatus: {overall.upper()}")
    
    # 8. Historie
    hist = update_history(overall in ["ok", "info"])
    u24, u7 = calc_uptime(hist)
    render_sparkline(hist)
    
    old_state = read_json(STATE_FILE, {"state": "ok"})["state"]
    if old_state != overall:
        print(f"  Status geändert: {old_state} → {overall}")
    write_json(STATE_FILE, {"state": overall})
    
    # =========================
    # Discord Embed
    # =========================
    emoji = {"ok": "🟢", "info": "🟡", "warn": "🟠", "error": "🔴", "unknown": "⚪"}
    
    title = f"{emoji.get(overall, '⚪')} Overwatch 2 Server Status"
    
    # Beschreibung
    desc_lines = []
    for region, data in regions.items():
        icon = emoji.get(data["status"], "⚪")
        if data["avg"]:
            desc_lines.append(f"{icon} **{region}**: {data['avg']}ms")
        else:
            desc_lines.append(f"{icon} **{region}**: Nicht erreichbar")
    
    if dd_reports and dd_reports > 10:
        desc_lines.append(f"\n⚠️ **User Reports**: {dd_reports} Meldungen")
    
    desc_lines.append(f"\n📊 **Uptime**: 24h: {u24}% • 7d: {u7}%")
    description = "\n".join(desc_lines)
    
    # Felder
    fields = []
    
    # Regionen Detail
    for region, data in regions.items():
        if data["avg"]:
            val = f"```\n📍 Latenz: {data['avg']}ms\n⚡ Min/Max: {data['min']}/{data['max']}ms\n🌐 Endpoints: {data['reachable']}/{data['total']}\n```"
        else:
            val = "```\n🔴 Keine Verbindung möglich\n```"
        fields.append({"name": f"🌍 {region} Region", "value": val, "inline": True})
    
    # Plattformen
    plat_text = "\n".join([f"{emoji.get(st, '⚪')} **{name}**: {st.upper()}" 
                           for name, st in platforms.items()])
    fields.append({"name": "🎮 Plattformen", "value": plat_text, "inline": False})
    
    # Wartung (nur wenn relevant)
    if has_maint or maint_status != "ok":
        fields.append({
            "name": "🔧 Wartungen",
            "value": f"{maint_msg}\n[Mehr Infos]({MAINT_URL})",
            "inline": False
        })
    
    # Patch (nur wenn vorhanden und relevant)
    if patch_title and has_patch:
        fields.append({
            "name": "📝 Neuester Patch",
            "value": f"**{patch_title}**\n{patch_date}\n[Patch Notes]({PATCH_URL})",
            "inline": False
        })
    
    # Known Issues
    if ki_count > 0:
        ki_text = f"**{ki_count} neue/aktualisierte Issues (24h)**\n"
        for issue in ki_list:
            url = f"https://us.forums.blizzard.com/en/overwatch/t/{issue['slug']}/{issue['id']}"
            ki_text += f"• [{issue['title'][:50]}...]({url})\n"
        ki_text += f"[Alle Issues]({FORUM_URL.replace('.json', '')})"
        fields.append({"name": "⚠️ Bekannte Probleme", "value": ki_text, "inline": False})
    
    embed = {
        "title": title,
        "description": description,
        "color": COLORS.get(overall, COLORS["unknown"]),
        "fields": fields,
        "footer": {"text": f"Letzte Prüfung: {now_utc_str()} • Status: {overall.upper()}"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    
    if THUMB_URL:
        embed["thumbnail"] = {"url": THUMB_URL}
    
    if REPO and SPARK_PATH.exists():
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{REPO}/main/assets/sparkline.png?t={int(time.time())}"}
    
    components = [{
        "type": 1,
        "components": [
            {"type": 2, "style": 5, "label": "🔧 Wartungen", "url": MAINT_URL},
            {"type": 2, "style": 5, "label": "📝 Patch Notes", "url": PATCH_URL},
            {"type": 2, "style": 5, "label": "⚠️ Known Issues", "url": FORUM_URL.replace(".json", "")},
            {"type": 2, "style": 5, "label": "💬 Support", "url": "https://support.blizzard.com"}
        ]
    }]
    
    payload = {"embeds": [embed], "components": components}
    
    # Diff-Check
    last = read_json(LAST_FILE, None)
    if last == payload:
        print("\n✅ Keine Änderungen, Update übersprungen")
        raise SystemExit(0)
    
    write_json(LAST_FILE, payload)
    
    # Discord Update
    print("\n📤 Sende Update an Discord...")
    mid = MID_FILE.read_text().strip() if MID_FILE.exists() else None
    
    if mid:
        r = edit_message(mid, payload)
        if r.status_code == 404:
            mid = None
        else:
            r.raise_for_status()
            print("✅ Nachricht aktualisiert")
    
    if not mid:
        new_id = send_message(payload)
        MID_FILE.write_text(str(new_id))
        print(f"✅ Neue Nachricht erstellt (ID: {new_id})")
    
    print("\n✅ Status-Check abgeschlossen!")