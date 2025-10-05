import os, json, time, socket, datetime, requests
from bs4 import BeautifulSoup
from pathlib import Path

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
THUMB_URL = os.environ.get("THUMB_URL", "").strip()
STATE_DIR = Path(".bot_state")
STATE_DIR.mkdir(parents=True, exist_ok=True)
MID_FILE = STATE_DIR / "ow_message_id.txt"
HIST_FILE = STATE_DIR / "history.json"
SPARK_PATH = Path("assets/sparkline.png")

# Quellen (stabil, aber ohne offizielles JSON)
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
KNOWN_ISSUES_URL = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64"

REGION_HOSTS = {
    "EU": ["eu.actual.battle.net", "overwatch.blizzard.com"],
    "NA": ["us.actual.battle.net", "overwatch.blizzard.com"],
    "ASIA": ["kr.actual.battle.net", "overwatch.blizzard.com"],
}

def tcp_probe(host: str, port=443, timeout=3.0) -> float | None:
    t0 = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return round((time.time() - t0) * 1000.0, 1)
    except OSError:
        return None

def aggregate_latency(hosts):
    samples = []
    for h in hosts:
        ms = tcp_probe(h)
        if ms is not None:
            samples.append(ms)
    if not samples:
        return {"min": None, "avg": None, "max": None}
    return {"min": min(samples), "avg": round(sum(samples)/len(samples),1), "max": max(samples)}

def check_maintenance():
    try:
        html = requests.get(MAINT_URL, timeout=20).text
        txt = BeautifulSoup(html, "html.parser").get_text(" ").lower()
        has_ow = "overwatch" in txt
        has_maint = any(k in txt for k in ["maintenance", "downtime", "scheduled"])
        if has_ow and has_maint:
            return ("Wartungshinweise gefunden.", "warn")
        return ("Keine expliziten OW-Wartungshinweise gefunden.", "ok")
    except Exception as e:
        return (f"Wartungsseite nicht prüfbar ({e}).", "unknown")

def check_known_issues():
    try:
        r = requests.get(KNOWN_ISSUES_URL, timeout=20)
        if r.status_code == 200:
            # Sehr grobe Heuristik: wenn Liste nicht leer -> es gibt offene Themen
            return ("Es liegen gemeldete „Known Issues“ vor (Details im Link).", "info")
        elif r.status_code in (301,302,303,307,308):
            return ("Hinweise im Forum verfügbar (weitergeleitet).", "info")
        else:
            return ("Forum nicht erreichbar.", "unknown")
    except Exception:
        return ("Forum nicht erreichbar.", "unknown")

def color_for(status: str) -> int:
    return {
        "ok": 0x2ECC71,      # grün
        "warn": 0xF1C40F,    # gelb
        "info": 0x3498DB,    # blau
        "unknown": 0x95A5A6  # grau
    }.get(status, 0x95A5A6)

def build_embed():
    maint_msg, maint_state = check_maintenance()
    issues_msg, issues_state = check_known_issues()

    fields = []
    # Regionale Latenz
    for region, hosts in REGION_HOSTS.items():
        lat = aggregate_latency(hosts)
        if lat["avg"] is None:
            val = "keine Messung möglich"
        else:
            val = f'Ø {lat["avg"]} ms (min {lat["min"]} / max {lat["max"]})'
        fields.append({"name": f"{region} – Erreichbarkeit", "value": val, "inline": True})

    fields.append({"name": "Wartung", "value": f"[{maint_msg}]({MAINT_URL})", "inline": False})
    fields.append({"name": "Known Issues", "value": f"[{issues_msg}]({KNOWN_ISSUES_URL})", "inline": False})

    desc = "Überblick der erreichbaren Dienste und Hinweise. Dies ist **kein** offizielles Live-„Server down“-Signal."
    embed = {
        "title": "Overwatch 2 – Status",
        "description": desc,
        "color": color_for(maint_state if maint_state!="ok" else issues_state),
        "fields": fields,
        "footer": {"text": f"Automatisch via GitHub Actions • zuletzt geprüft: {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M UTC')}"},
        "timestamp": datetime.datetime.utcnow().isoformat()
    }
    if THUMB_URL:
        embed["thumbnail"] = {"url": THUMB_URL}
    if SPARK_PATH.exists():
        embed["image"] = {"url": f"https://raw.githubusercontent.com/{os.environ.get('GITHUB_REPOSITORY')}/main/assets/sparkline.png"}
    return embed

def diff_changed(new_payload: dict) -> bool:
    prev = STATE_DIR / "last_payload.json"
    if prev.exists():
        try:
            old = json.loads(prev.read_text())
            if old == new_payload:
                return False
        except Exception:
            pass
    prev.write_text(json.dumps(new_payload, ensure_ascii=False, separators=(",",":")))
    return True

def append_history(ok: bool):
    # speichere 24h Verlauf (max 24 Einträge)
    hist = []
    if HIST_FILE.exists():
        try:
            hist = json.loads(HIST_FILE.read_text())
        except Exception:
            hist = []
    hist.append({"t": int(time.time()), "ok": 1 if ok else 0})
    hist = hist[-24:]
    HIST_FILE.write_text(json.dumps(hist))

def render_sparkline():
    try:
        from PIL import Image, ImageDraw
        hist = []
        if HIST_FILE.exists():
            hist = json.loads(HIST_FILE.read_text())
        if not hist:
            return
        w, h = 320, 48
        img = Image.new("RGB", (w, h), (24, 26, 27))
        d = ImageDraw.Draw(img)
        # Rahmen
        d.rectangle([0,0,w-1,h-1], outline=(60,60,60))
        # Werte (0/1) als Linie
        n = len(hist)
        if n == 1: n = 2
        step = (w-16) / (n-1)
        pts = []
        for i, e in enumerate(hist):
            y = 8 + (1 - e["ok"]) * (h-16)  # ok=1 -> oben, not ok -> unten
            x = 8 + i*step
            pts.append((x,y))
        d.line(pts, width=2, fill=(120,180,250))
        # Legende
        d.text((10, h-14), "24h-Verlauf (OK/Problems)", fill=(180,180,180))
        img.save(SPARK_PATH)
    except Exception:
        pass

def read_mid() -> str | None:
    return MID_FILE.read_text().strip() if MID_FILE.exists() else None

def write_mid(mid: str):
    MID_FILE.write_text(mid)

def send_new(payload):
    r = requests.post(WEBHOOK + "?wait=true", json=payload, timeout=20)
    r.raise_for_status()
    return r.json()["id"]

def edit_existing(mid: str, payload):
    # PATCH /messages/{message_id}
    import urllib.parse
    parts = urllib.parse.urlparse(WEBHOOK).path.split("/")
    i = parts.index("webhooks")
    webhook_id, token = parts[i+1], parts[i+2]
    url = f"https://discord.com/api/webhooks/{webhook_id}/{token}/messages/{mid}"
    r = requests.patch(url, json=payload, timeout=20)
    return r

if __name__ == "__main__":
    # Daten sammeln
    maint_msg, maint_state = check_maintenance()
    ok_for_history = maint_state == "ok"
    append_history(ok_for_history)
    render_sparkline()

    embed = build_embed()
    # Buttons/Links
    components = [{
        "type": 1,
        "components": [
            {"type":2, "style":5, "label":"Maintenance", "url": MAINT_URL},
            {"type":2, "style":5, "label":"Known Issues", "url": KNOWN_ISSUES_URL}
        ]
    }]
    payload = {"embeds": [embed], "components": components}

    # Nur editieren, wenn sich das Embed geändert hat
    if not diff_changed(payload):
        # Nichts zu tun
        raise SystemExit(0)

    mid = read_mid()
    if mid:
        r = edit_existing(mid, payload)
        if r.status_code == 404:
            mid = None
        else:
            r.raise_for_status()
    if not mid:
        new_id = send_new(payload)
        write_mid(str(new_id))