# =========================
# Plattform-Status (konservativ)
# =========================
PLATFORM_CACHE = STATE_DIR / "platform_cache.json"

def _read_cache():
    return read_json(PLATFORM_CACHE, {
        "PC": {}, "PlayStation": {}, "Xbox": {}, "Switch": {}
    })

def _write_cache(c):
    write_json(PLATFORM_CACHE, c)

def _minutes_ago(ts):
    if not ts: return None
    try:
        return int((time.time() - ts) / 60)
    except Exception:
        return None

def platform_icon(state: str) -> str:
    return {"ok":"🟢","info":"🟡","warn":"🔴","unknown":"⚪️"}.get(state, "⚪️")

def _try_json(url, path_keys, ok_vals=("up","normal","operational"), warn_vals=("degraded","limited","maintenance"), bad_vals=("down","outage","trouble"), timeout=8):
    """Sehr robuste JSON-Prüfung. Gibt (state|None, source) zurück."""
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
        r.raise_for_status()
        data = r.json()
        # path_keys z.B. ["status"] oder ["services","0","state"]
        cur = data
        for k in path_keys:
            if isinstance(cur, list):
                k = int(k)
            cur = cur[k]
        s = str(cur).strip().lower()
        if any(v in s for v in bad_vals):  return ("warn",  "json")
        if any(v in s for v in warn_vals): return ("info",  "json")
        if any(v in s for v in ok_vals):   return ("ok",    "json")
        return (None,      "json")
    except Exception:
        return (None, "json")

def _try_html(url, ok_kw, warn_kw, bad_kw, timeout=8):
    """Fallback: HTML-Keywords. Gibt (state|None, source) zurück."""
    try:
        r = requests.get(url, timeout=timeout, headers=UA)
        r.raise_for_status()
        t = (r.text or "").lower()
        if any(k in t for k in bad_kw):  return ("warn",  "html")
        if any(k in t for k in warn_kw): return ("info",  "html")
        if any(k in t for k in ok_kw):   return ("ok",    "html")
        return (None, "html")
    except Exception:
        return (None, "html")

def _quorum_merge(candidates):
    """
    candidates = [(state or None, source_str), ...]
    Regel: warn nur bei klarer Bestätigung; sonst info; sonst ok; sonst unknown.
    Priorität: ok > info > warn bei Widerspruch (konservativ).
    """
    states = [s for s,_ in candidates if s is not None]
    if not states:
        return "unknown", []
    # Wenn mind. ein OK: OK (weil Nutzer wirklich spielen kann)
    if "ok" in states:
        used = [src for s,src in candidates if s == "ok"]
        return "ok", used
    # Kein ok, aber Infos: INFO (Hinweis/Maintenance)
    if "info" in states:
        used = [src for s,src in candidates if s == "info"]
        return "info", used
    # Nur warn übrig? Dann warn.
    if "warn" in states:
        used = [src for s,src in candidates if s == "warn"]
        return "warn", used
    return "unknown", []

def robust_platform_status_overview(pc_state: str):
    """
    Liefert dict: { name: (state, note, url, from_cache_minutes|None) }
    Konservativ, mit Cache & Quorum; niemals rot ohne harte Bestätigung.
    """
    cache = _read_cache()
    out = {}

    # 0) PC = dein zusammengefasster Heuristik-Status
    out["PC"] = (pc_state, "Overwatch Reachability", "https://overwatch.blizzard.com", None)
    cache["PC"] = {"state": pc_state, "ts": time.time()}

    # 1) PlayStation
    ps_candidates = []
    # a) (wenn verfügbar) JSON-API – hier KEINE fixe URL vorausgesetzt; oft 403/JS
    # -> wir verlassen uns primär auf die HTML-Variante; JSON bleibt optional
    # ps_candidates.append(_try_json("https://status.playstation.com/api/v1/status", ["status"]))
    # b) HTML-Fallback
    ps_candidates.append(_try_html(
        "https://status.playstation.com",
        ok_kw=["all services are up","no issues","up and running","services are available"],
        warn_kw=["limited","degraded","maintenance","some services"],
        bad_kw=["major outage","outage","down","service is down"]
    ))
    ps_state, ps_srcs = _quorum_merge(ps_candidates)
    if ps_state == "unknown":
        # Cache verwenden, aber als cached kennzeichnen
        prev = cache.get("PlayStation", {})
        ps_state = prev.get("state", "unknown")
        age = _minutes_ago(prev.get("ts"))
        out["PlayStation"] = (ps_state, "PSN (cached)" if age else "PSN", "https://status.playstation.com", age)
    else:
        out["PlayStation"] = (ps_state, "PSN ("+",".join(ps_srcs)+")", "https://status.playstation.com", None)
        cache["PlayStation"] = {"state": ps_state, "ts": time.time()}

    # 2) Xbox
    xb_candidates = []
    # xb_candidates.append(_try_json("https://xnotify.xboxlive.com/api/health", ["state"]))
    xb_candidates.append(_try_html(
        "https://support.xbox.com/en-US/xbox-live-status",
        ok_kw=["all services up","no problems","up and running","services are available"],
        warn_kw=["limited","degraded","maintenance"],
        bad_kw=["major outage","outage","down"]
    ))
    xb_state, xb_srcs = _quorum_merge(xb_candidates)
    if xb_state == "unknown":
        prev = cache.get("Xbox", {})
        xb_state = prev.get("state", "unknown")
        age = _minutes_ago(prev.get("ts"))
        out["Xbox"] = (xb_state, "Xbox Live (cached)" if age else "Xbox Live", "https://support.xbox.com/en-US/xbox-live-status", age)
    else:
        out["Xbox"] = (xb_state, "Xbox Live ("+",".join(xb_srcs)+")", "https://support.xbox.com/en-US/xbox-live-status", None)
        cache["Xbox"] = {"state": xb_state, "ts": time.time()}

    # 3) Nintendo
    nin_candidates = []
    # nin_candidates.append(_try_json("https://www.nintendo.co.jp/netinfo/en_US/json/system_status.json", ["operational_status"]))
    nin_candidates.append(_try_html(
        "https://www.nintendo.co.jp/netinfo/en_US/index.html",
        ok_kw=["operating normally","all servers are operating normally","no issues"],
        warn_kw=["maintenance","under maintenance","scheduled maintenance"],
        bad_kw=["experiencing issues","service outage","outage","down"]
    ))
    nin_state, nin_srcs = _quorum_merge(nin_candidates)
    if nin_state == "unknown":
        prev = cache.get("Switch", {})
        nin_state = prev.get("state", "unknown")
        age = _minutes_ago(prev.get("ts"))
        out["Switch"] = (nin_state, "Nintendo Online (cached)" if age else "Nintendo Online", "https://www.nintendo.co.jp/netinfo/en_US/index.html", age)
    else:
        out["Switch"] = (nin_state, "Nintendo Online ("+",".join(nin_srcs)+")", "https://www.nintendo.co.jp/netinfo/en_US/index.html", None)
        cache["Switch"] = {"state": nin_state, "ts": time.time()}

    _write_cache(cache)
    return out