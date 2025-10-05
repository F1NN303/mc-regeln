import os, requests, datetime
from bs4 import BeautifulSoup

WEBHOOK = os.environ["DISCORD_WEBHOOK_URL"]
MAINT_URL = "https://eu.support.blizzard.com/en/article/000358479"  # Weekly Maintenance (EN-GB)

def get_maintenance_summary():
    try:
        html = requests.get(MAINT_URL, timeout=20).text
        soup = BeautifulSoup(html, "html.parser")
        # sehr einfacher Heuristik-Check: Seite enthält "Weekly Maintenance" + "Overwatch"
        text = soup.get_text(separator=" ").lower()
        if "overwatch" in text:
            return "⚠️ Geplante Wartung für Overwatch gefunden. Details im Link."
        return "✅ Keine expliziten OW-Wartungshinweise gefunden."
    except Exception as e:
        return f"❓ Konnte Wartungsseite nicht prüfen ({e})."

def send_discord(msg):
    payload = {
        "username": "OW2 Status Bot",
        "embeds": [{
            "title": "Overwatch 2 – Status-Update",
            "description": msg,
            "color": 0xF99A00,
            "fields": [
                {"name": "Offizielle Wartungsseite", "value": f"[Weekly Maintenance]({MAINT_URL})", "inline": False},
                {"name": "Support/Forum/Hinweise", "value": "Overwatch Forum / BlizzardCS auf X", "inline": False},
            ],
            "footer": {"text": "Automatisch via GitHub Actions"},
            "timestamp": datetime.datetime.utcnow().isoformat()
        }]
    }
    r = requests.post(WEBHOOK, json=payload, timeout=20)
    r.raise_for_status()

if __name__ == "__main__":
    summary = get_maintenance_summary()
    send_discord(summary)
