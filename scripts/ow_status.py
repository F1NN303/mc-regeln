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

UA = {"User-Agent": "OW2-Status/1.3 (+github-actions)"}

# =========================
# Farbdefinitionen & Schwellen
# =========================
COLORS = {"ok": 0x2ECC71, "info": 0x3498DB, "warn": 0xF1C40F, "unknown": 0x95A5A6}
ORDER  = {"ok":0, "info":1, "warn":2, "unknown":3}
INFO_MS = float(os.environ.get("INFO_MS", "200"))
WARN_MS = float(os.environ.get("WARN_MS", "400"))

# =========================
# Hilfsfunktionen
# =========================
def now_utc():     return datetime.datetime.now(datetime.UTC)
def now_utc_str(): return now_utc().strftime("%Y-%m-%d %H:%M UTC")

def read_json(p: Path, default):
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return default

def write_json(p: Path, obj):
    p.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":")), encoding="utf-8")

# =========================
# Messung: TCP / Ping
# =========================
REGION_HOSTS = {
    "EU":   ["eu.actual.battle.net", "overwatch.blizzard.com"],
    "NA":   ["us.actual.battle.net", "overwatch.blizzard.com"],
    "ASIA": ["kr.actual.battle.net", "overwatch.blizzard.com"],
}
REGIONS = [r for r in REGIONS if r in REGION_HOSTS] or ["EU","NA","ASIA"]

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
# Quellen (Maintenance + Known Issues)
# =========================
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"
KNOWN_ISSUES_JSON = "https://us.forums.blizzard.com/en/overwatch/c/overwatch-2/known-issues/64.json"
MAINT_DATE_RE = re.compile(r"(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\w*,?\s*)?(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*[, ]+\s*(\d{4})", re.I)

def fetch_known_issues_summary():
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

def fetch_maintenance_hint():
    try:
        html = requests.get(MAINT_URL, timeout=20, headers=UA).text
        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text(" ")
        lw = text.lower()
        if "overwatch" in lw and any(k in lw for k in ["maintenance","downtime","scheduled"]):
            m = MAINT_DATE_RE.search(text)
            when = f"{m.group(2)} {m.group(3)} {m.group(4)}" if m else "Termin auf Seite"
            return "warn", f"Wartungshinweis gefunden ({when})."
        return "ok", "Keine expliziten OW-Wartungshinweise."
    except Exception:
        return "unknown", "Wartungsseite nicht prüfbar."

# =========================
# Plattform-Status (konservativ + Cache)
# =========================
def _read_cache():
    return read_json(PLATFORM_CACHE, {"PC": {}, "PlayStation": {}, "Xbox": {}, "Switch": {}})

def _write_cache(c):
    write_json(PLATFORM_CACHE, c)

def _minutes_ago(ts):
    if not ts: return None
    try: return int((time.time() - ts) / 60)
    except Exception: return None

def platform_icon(state: str) -> str:
    return {"ok":"🟢","info":"🟡","warn":"🔴","unknown":"⚪️"}.get(state, "⚪️")

def _try_html(url, ok_kw, warn_kw, bad_kw, timeout=8):
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
        r.raise_for_status()
        t = (r.text or "").lower()
        if any(k in t for k in bad_kw):  return ("warn","html")
        if any(k in t for k in warn_kw): return ("info","html")
        if any(k in t for k in ok_kw):   return ("ok","html")
        return (None,"html")
    except Exception:
        return (None,"html")

def _quorum_merge(candidates):
    states = [s for s,_ in candidates if s is not None]
    if not states: return "unknown", []
    if "ok" in states:   return "ok",   [src for s,src in candidates if s=="ok"]
    if "info" in states: return "info", [src for s,src in candidates if s=="info"]
    if "warn" in states: return "warn", [src for s,src in candidates if s=="warn"]
    return "unknown", []

def robust_platform_status_overview(pc_state: str):
    cache = _read_cache()
    out = {}

    out["PC"] = (pc_state, "Overwatch Reachability", "https://overwatch.blizzard.com", None)
    cache["PC"] = {"state": pc_state, "ts": time.time()}

    # PlayStation
    ps_candidates = [
        _try_html(
            "https://status.playstation.com",
            ok_kw=["all services are up","no issues","up and running"],
            warn_kw=["limited","degraded","maintenance"],
            bad_kw=["major outage","outage","down"]
        )
    ]
    ps_state, _ = _quorum_merge(ps_candidates)
    if ps_state == "unknown":
        prev = cache.get("PlayStation", {})
        ps_state = prev.get("state", "unknown")
        age = _minutes_ago(prev.get("ts"))
        out["PlayStation"] = (ps_state,"PSN (cached)" if age else "PSN","https://status.playstation.com",age)
    else:
        out["PlayStation"] = (ps_state,"PSN","https://status.playstation.com",None)
        cache["PlayStation"] = {"state": ps_state,"ts": time.time()}

    # Xbox
    xb_candidates = [
        _try_html(
            "https://support.xbox.com/en-US/xbox-live-status",
            ok_kw=["all services up","no problems","up and running"],
            warn_kw=["limited","degraded","maintenance"],
            bad_kw=["outage","down"]
        )
    ]
    xb_state, _ = _quorum_merge(xb_candidates)
    if xb_state == "unknown":
        prev = cache.get("Xbox", {})
        xb_state = prev.get("state","unknown")
        age = _minutes_ago(prev.get("ts"))
        out["Xbox"] = (xb_state,"Xbox Live (cached)" if age else "Xbox Live","https://support.xbox.com/en-US/xbox-live-status",age)
    else:
        out["Xbox"] = (xb_state,"Xbox Live","https://support.xbox.com/en-US/xbox-live-status",None)
        cache["Xbox"] = {"state": xb_state,"ts": time.time()}

    # Nintendo
    nin_candidates = [
        _try_html(
            "https://www.nintendo.co.jp/netinfo/en_US/index.html",
            ok_kw=["operating normally","no issues"],
            warn_kw=["maintenance","under maintenance"],
            bad_kw=["outage","down","experiencing issues"]
        )
    ]
    nin_state, _ = _quorum_merge(nin_candidates)
    if nin_state == "unknown":
        prev = cache.get("Switch", {})
        nin_state = prev.get("state","unknown")
        age = _minutes_ago(prev.get("ts"))
        out["Switch"] = (nin_state,"Nintendo (cached)" if age else "Nintendo","https://www.nintendo.co.jp/netinfo/en_US/index.html",age)
    else:
        out["Switch"] = (nin_state,"Nintendo","https://www.nintendo.co.jp/netinfo/en_US/index.html",None)
        cache["Switch"] = {"state": nin_state,"ts": time.time()}

    _write_cache(cache)
    return out

# =========================
# Verlauf / Sparkline / Changelog
# =========================
def append_history(is_ok: bool):
    hist = read_json(HIST_FILE, [])
    hist.append({"t": int(time.time()), "ok": 1 if is_ok else 0})
    hist = hist[-168:]
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
        pts=[(8+i*step, 10+(1-e["ok"])*(h-20)) for i,e in enumerate(hist)]
        d.line(pts,width=2)
        u24,u7 = uptimes(hist)
        d.text((10,h-14),f"Uptime 24h: {u24}% • 7T: {u7}%",fill=(200,200,200))
        SPARK_PATH.parent.mkdir(parents=True,exist_ok=True)
        img.save(SPARK_PATH)
    except Exception:
        pass

def save_changelog_change(old_state,new_state):
    if old_state==new_state: return
    cl = read_json(CHANGELOG,[])
    cl.append({"t": now_utc_str(), "from": old_state, "to": new_state})
    write_json(CHANGELOG,cl[-6:])

def last_changelog_lines(n=2):
    cl = read_json(CHANGELOG,[])
    if not cl: return "—"
    return " • ".join(f"{e['t']} → {e['to'].upper()}" for e in cl[-n:])

# =========================
# Discord I/O
# =========================
def parse_webhook():
    from urllib.parse import urlparse
    parts = urlparse(WEBHOOK).path.strip("/").split("/")
    i = parts.index("webhooks")
    return parts[i+1], parts[i+2]

def discord_request(method,url,json_payload):
    for _ in range(4):
        r = requests.request(method,url,json=json_payload,timeout=20,headers=UA)
        if r.status_code!=429: return r
        time.sleep(min(max(float(r.headers.get("Retry-After","1")),1),10))
    return r

def send_new(payload):
    r = discord_request("POST",WEBHOOK+"?wait=true",payload)
    r.raise_for_status()
    return r.json()["id"]

def edit_existing(mid,payload):
    wid,tok = parse_webhook()
    url=f"https://discord.com/api/webhooks/{wid}/{tok}/messages/{mid}"
    return discord_request("PATCH",url,payload)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    # Regionen prüfen
    regions = {r: aggregate_region(REGION_HOSTS[r]) for r in REGIONS}
    last_lat = read_json(LAT_FILE,{})
    trends={}
    for r in REGIONS:
        prev=last_lat.get(r,{}).get("avg")
        cur=regions[r]["avg"]
        if prev is None or cur is None: trends[r]="•"
        else: trends[r]="▲" if cur>(prev+20) else ("▼" if cur<(prev-20) else "→")
    write_json(LAT_FILE,regions)

    maint_state,maint_msg=fetch_maintenance_hint()
    ki_count,ki_title,ki_url=fetch_known_issues_summary()

    parts=[maint_state]+[severity_from_latency(regions[r]["avg"]) for r in REGIONS]
    if ki_count and ki_count>0: parts.append("info")
    new_state=worst_state(parts)

    hist=append_history(new_state=="ok")
    u24,u7=uptimes(hist)
    render_sparkline(hist)

    old_state=read_json(STATE_FILE,{"state":"ok"})["state"]
    if old_state!=new_state:
        save_changelog_change(old_state,new_state)
        write_json(STATE_FILE,{"state":new_state})

    head_bits=[f"{r} Ø{regions[r]['avg']:.0f}ms" if regions[r]['avg'] else f"{r} n/a" for r in REGIONS]
    head_line="   ".join(head_bits)+f" | 24h {u24}% • 7T {u7}%"
    description=f"```\n{head_line}\n```"

    # Plattformen (konservativ, mit Cache/Fallback)
    platforms=robust_platform_status_overview(new_state)
    lines=[]
    for name in ("PC","PlayStation","Xbox","Switch"):
        st,note,link,age=platforms[name]
        age_txt=f" (cached {age}m)" if age else ""
        lines.append(f"{name:<11} {platform_icon(st)} {st.upper():<7}{age_txt}")
    platform_block="```\n"+"\n".join(lines)+"\n```"

    # Embed-Felder
    fields=[]
    fields.append({"name":"Plattformen","value":platform_block,"inline":False})
    for r in REGIONS:
        v=regions[r]
        val="keine Messung" if v["avg"] is None else f"Ø {v['avg']} ms ({v['min']}/{v['max']}) {trends[r]}"
        fields.append({"name":f"{r} – Erreichbarkeit","value":val,"inline":True})
    fields.append({"name":"Wartung","value":f"[{maint_msg}]({MAINT_URL})","inline":False})
    if ki_count is None:
        ki_val=f"[Keine Daten]({ki_url})"
    else:
        label=f"{ki_count} neue/aktualisierte Beiträge in 24h" if ki_count>0 else "Keine neuen Beiträge in 24h"
        ki_val=f"[{label}]({ki_url})"+(f"\nZuletzt: „{ki_title}“" if ki_title else "")
    fields.append({"name":"Known Issues","value":ki_val,"inline":False})
    fields.append({"name":"Letzte Änderungen","value":last_changelog_lines(2),"inline":False})

    embed={
        "title":"Overwatch 2 – Status",
        "description":description,
        "color":COLORS.get(new_state,COLORS["unknown"]),
        "fields":fields,
        "footer":{"text":f"Letzte Prüfung: {now_utc_str()}"},
        "timestamp":datetime.datetime.utcnow().isoformat()
    }
    if THUMB_URL:
        embed["thumbnail"]={"url":THUMB_URL}
    if REPO and SPARK_PATH.exists():
        embed["image"]={"url":f"https://raw.githubusercontent.com/{REPO}/main/assets/sparkline.png"}

    components=[{
        "type":1,
        "components":[
            {"type":2,"style":5,"label":"Maintenance","url":MAINT_URL},
            {"type":2,"style":5,"label":"Known Issues","url":ki_url},
            {"type":2,"style":5,"label":"Support","url":"https://support.blizzard.com"}
        ]
    }]

    payload={"embeds":[embed],"components":components}

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