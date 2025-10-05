import os, json, datetime, requests
from urllib.parse import urlparse
from bs4 import BeautifulSoup

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
STATE_FILE = ".bot_state/ow_message_id.txt"
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"

def get_maintenance_summary():
    try:
        html = requests.get(MAINT_URL, timeout=20).text
        txt = BeautifulSoup(html, "html.parser").get_text(" ").lower()
        if "overwatch" in txt and ("maintenance" in txt or "downtime" in txt):
            return "⚠️ Geplante Wartungshinweise für Overwatch gefunden. Details unten."
        return "✅ Keine expliziten OW-Wartungshinweise gefunden."
    except Exception as e:
        return f"❓ Wartungsseite nicht prüfbar ({e})."

def build_embed(msg: str):
    return {
        "embeds": [{
            "title": "Overwatch 2 – Status-Update",
            "description": msg,
            "fields": [
                {"name": "Offizielle Wartung", "value": f"[Weekly Maintenance]({MAINT_URL})", "inline": False},
            ],
            "footer": {"text": "Automatisch via GitHub Actions"},
            "timestamp": datetime.datetime.utcnow().isoformat()
        }]
    }

def parse_webhook(url: str):
    # https://discord.com/api/webhooks/{webhook_id}/{token}
    p = urlparse(url)
    parts = p.path.strip("/").split("/")
    try:
        i = parts.index("webhooks")
        return parts[i+1], parts[i+2]
    except Exception:
        raise ValueError("Ungültige DISCORD_WEBHOOK_URL")

def send_new(url: str, payload: dict) -> str:
    # ?wait=true => Message-Objekt zurückbekommen
    r = requests.post(url + "?wait=true", json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    return str(data["id"])

def edit_existing(webhook_id: str, token: str, message_id: str, payload: dict):
    edit_url = f"https://discord.com/api/webhooks/{webhook_id}/{token}/messages/{message_id}"
    r = requests.patch(edit_url, json=payload, timeout=20)
    if r.status_code == 404:
        raise FileNotFoundError("Message nicht mehr vorhanden (404).")
    r.raise_for_status()

def read_message_id() -> str | None:
    if os.path.exists(STATE_FILE):
        return open(STATE_FILE, "r").read().strip() or None
    return None

def write_message_id(mid: str):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        f.write(mid)

if __name__ == "__main__":
    msg = get_maintenance_summary()
    payload = build_embed(msg)

    webhook_id, token = parse_webhook(WEBHOOK)
    mid = read_message_id()

    try:
        if mid:
            # vorhandene Nachricht bearbeiten
            edit_existing(webhook_id, token, mid, payload)
        else:
            # erste Nachricht erstellen und ID speichern
            new_id = send_new(WEBHOOK, payload)
            write_message_id(new_id)
    except FileNotFoundError:
        # Alte Message existiert nicht mehr → neu senden und ID ersetzen
        new_id = send_new(WEBHOOK, payload)
        write_message_id(new_id)
    except requests.HTTPError as e:
        # Fallback: bei 400/401 etc. neu versuchen (z.B. wenn Embed-Format geändert)
        if e.response is not None and e.response.status_code in (400, 401, 403):
            new_id = send_new(WEBHOOK, payload)
            write_message_id(new_id)
        else:
            raise