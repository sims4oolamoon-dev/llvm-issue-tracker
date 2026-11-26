import html
import json
import os
from http.server import BaseHTTPRequestHandler
import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])
STATE_RAW_URL = os.environ["STATE_RAW_URL"]  # e.g. https://raw.githubusercontent.com/<user>/<repo>/main/state.json

HELP_TEXT = """<b>🤖 LLVM Issue Tracker (Instant Commands)</b>

<code>/unclaimed</code> — 5 newest unclaimed issues
<code>/unclaimed clang</code> — Unclaimed issues with tag <code>clang</code>
<code>/stale</code> — 5 stale issues (&gt;7 days idle, no PR)
<code>/stale llvm:transforms</code> — Stale issues with tag
<code>/status</code> — Summary counts &amp; last scan time
<code>/help</code> — Show this menu"""


def send_telegram(text: str) -> None:
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )


def fetch_state() -> dict:
    resp = requests.get(STATE_RAW_URL, timeout=10)
    resp.raise_for_status()
    return resp.json()


def format_card(badge: str, info: dict) -> str:
    title = html.escape(info["title"])
    tags = ", ".join(f"<code>{html.escape(l)}</code>" for l in info["labels"]) or "None"
    extra = f"\n<b>Idle:</b> {info['days_idle']} days (no open PR)" if info["status"] == "stale" else ""
    return (
        f"<b>{badge}</b>\n"
        f"<b>#{info['number']}:</b> {title}\n"
        f"<b>Opened:</b> {info['created_at'][:10]}\n"
        f"<b>Tags:</b> {tags}{extra}\n"
        f"<a href=\"{info['url']}\">Open on GitHub</a>"
    )


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or "{}")
        msg = body.get("message") or {}

        if str(msg.get("chat", {}).get("id")) != TELEGRAM_CHAT_ID:
            self.send_response(200)
            self.end_headers()
            return

        text = (msg.get("text") or "").strip()
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower() if parts else ""
        tag_filter = parts[1].strip().lower() if len(parts) > 1 else ""

        if cmd in ("/help", "/start"):
            send_telegram(HELP_TEXT)

        elif cmd == "/status":
            state = fetch_state()
            issues = list(state.get("issues", {}).values())
            unclaimed = sum(1 for i in issues if i["status"] == "unclaimed")
            stale = sum(1 for i in issues if i["status"] == "stale")
            send_telegram(
                f"<b>📊 LLVM Tracker Status</b>\n"
                f"<b>Last scan:</b> {state.get('last_scan', 'Never')}\n"
                f"<b>Total open:</b> {len(issues)}\n"
                f"<b>Unclaimed:</b> {unclaimed}\n"
                f"<b>Stale (&gt;7d, no PR):</b> {stale}"
            )

        elif cmd in ("/unclaimed", "/stale"):
            target = "unclaimed" if cmd == "/unclaimed" else "stale"
            badge = "📌 Unclaimed Issue" if target == "unclaimed" else "⏳ Stale Issue (>7d)"
            state = fetch_state()
            issues = [
                i for i in state.get("issues", {}).values()
                if i["status"] == target
                and (not tag_filter or any(tag_filter in l.lower() for l in i["labels"]))
            ]
            # Sort newest first for unclaimed, most idle first for stale
            issues.sort(
                key=lambda x: x["days_idle"] if target == "stale" else x["created_at"],
                reverse=True,
            )
            if not issues:
                send_telegram(f"No <b>{target}</b> issues found" + (f" matching <code>{html.escape(tag_filter)}</code>." if tag_filter else "."))
            else:
                for item in issues[:5]:
                    send_telegram(format_card(badge, item))

        self.send_response(200)
        self.end_headers()
