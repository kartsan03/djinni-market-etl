"""
telegram_alert.py — lightweight Telegram heartbeat for the scrapers and pipeline.

Env variables (.env in the repo root):
  TELEGRAM_BOT_TOKEN — bot token (placeholder; when unset, alerts are a no-op)
  TELEGRAM_CHAT_ID   — recipient chat_id

Guarantees: NEVER raises (an alert must never take down a scraper),
never blocks for more than 10 seconds.
"""
import os

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
load_dotenv()

ICONS = {"success": "✅", "partial": "⚠️", "failed": "💀", "info": "ℹ️"}


def send_heartbeat(source: str, status: str, detail: str = "") -> None:
    """Sends a heartbeat. status: success | partial | failed | info."""
    try:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            # Placeholder mode: log to stdout, send nothing.
            print(f"[heartbeat:no-config] {source}: {status} {detail}".strip())
            return

        icon = ICONS.get(status, "🔔")
        text = f"{icon} {source}: {status}"
        if detail:
            text += f"\n{detail}"

        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000]},
            timeout=10,
        )
    except Exception as exc:  # deliberately broad except: alert != crash
        print(f"[heartbeat:error] {exc}")
