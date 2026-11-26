import html
import json
import os
import re
from datetime import datetime, timezone, timedelta
import requests

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = str(os.environ["TELEGRAM_CHAT_ID"])

STATE_FILE = "state.json"

CLAIM_PATTERN = re.compile(
    r"\b(work(ing)? on|assign( me)?|take this|claim|i('d| would) like to)\b",
    re.IGNORECASE,
)

GRAPHQL_QUERY = """
query {
  repository(owner: "llvm", name: "llvm-project") {
    issues(
      first: 30
      states: OPEN
      labels: ["good first issue"]
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      nodes {
        number
        title
        url
        createdAt
        updatedAt
        labels(first: 10) {
          nodes { name }
        }
        assignees(first: 5) {
          nodes { login }
        }
        comments(last: 5) {
          nodes {
            author { login }
            createdAt
            bodyText
          }
        }
        timelineItems(last: 20, itemTypes: [CROSS_REFERENCED_EVENT]) {
          nodes {
            ... on CrossReferencedEvent {
              source {
                ... on PullRequest {
                  number
                  state
                  url
                }
              }
            }
          }
        }
      }
    }
  }
}
"""

HELP_TEXT = """<b>🤖 LLVM Issue Tracker Commands</b>

<code>/help</code> — Show this message
<code>/status</code> — Show current filters &amp; settings
<code>/list</code> — Show 5 latest unassigned issues matching your tags
<code>/addtag &lt;label&gt;</code> — Only track issues with this label (e.g. <code>/addtag llvm:transforms</code>)
<code>/removetag &lt;label&gt;</code> — Remove a label filter
<code>/cleartags</code> — Clear all label filters (track everything)
<code>/stale &lt;days&gt;</code> — Set stale threshold in days (default: 14)
<code>/pause</code> — Pause notifications
<code>/resume</code> — Resume notifications

<i>Note: Commands are processed on the next 30-minute check.</i>"""


def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def format_issue(badge: str, issue: dict, extra_note: str = "") -> str:
    title = html.escape(issue["title"])
    created = datetime.fromisoformat(issue["createdAt"].replace("Z", "+00:00"))
    created_str = created.strftime("%Y-%m-%d %H:%M UTC")
    labels = [lbl["name"] for lbl in issue["labels"]["nodes"]]
    tags_str = ", ".join(f"<code>{html.escape(l)}</code>" for l in labels) or "None"

    lines = [
        f"<b>{badge}</b>",
        f"<b>#{issue['number']}:</b> {title}",
        f"<b>Opened:</b> {created_str}",
        f"<b>Tags:</b> {tags_str}",
    ]
    if extra_note:
        lines.append(f"<b>Note:</b> {html.escape(extra_note)}")
    lines.append(f"<a href=\"{issue['url']}\">Open on GitHub</a>")
    return "\n".join(lines)


def fetch_issues() -> list[dict]:
    resp = requests.post(
        "https://api.github.com/graphql",
        json={"query": GRAPHQL_QUERY},
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")
    return data["data"]["repository"]["issues"]["nodes"]


def load_state() -> dict:
    default = {
        "last_update_id": 0,
        "config": {"tags": [], "stale_days": 14, "paused": False},
        "issues": {},
    }
    if not os.path.exists(STATE_FILE):
        return default
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Ensure keys exist
    default.update(data)
    return default


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def matches_tags(issue: dict, tags: list[str]) -> bool:
    if not tags:
        return True
    issue_labels = {lbl["name"].lower() for lbl in issue["labels"]["nodes"]}
    return any(t.lower() in issue_labels for t in tags)


def process_commands(state: dict, issues: list[dict]) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": state["last_update_id"] + 1, "timeout": 5}
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    updates = resp.json().get("result", [])

    cfg = state["config"]

    for update in updates:
        state["last_update_id"] = max(state["last_update_id"], update["update_id"])
        msg = update.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TELEGRAM_CHAT_ID:
            continue  # Ignore messages from anyone else

        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/help" or cmd == "/start":
            send_telegram(HELP_TEXT)

        elif cmd == "/status":
            tags_str = ", ".join(f"<code>{html.escape(t)}</code>" for t in cfg["tags"]) or "All (no filter)"
            status_str = "⏸ Paused" if cfg["paused"] else "▶️ Active"
            send_telegram(
                f"<b>📊 Tracker Status</b>\n"
                f"<b>State:</b> {status_str}\n"
                f"<b>Tag filters:</b> {tags_str}\n"
                f"<b>Stale threshold:</b> {cfg['stale_days']} days\n"
                f"<b>Tracked issues:</b> {len(state['issues'])}"
            )

        elif cmd == "/addtag":
            if not arg:
                send_telegram("Usage: <code>/addtag llvm:transforms</code>")
            elif arg.lower() not in [t.lower() for t in cfg["tags"]]:
                cfg["tags"].append(arg)
                send_telegram(f"✅ Added tag filter: <code>{html.escape(arg)}</code>")
            else:
                send_telegram(f"Tag <code>{html.escape(arg)}</code> is already in your filters.")

        elif cmd == "/removetag":
            before = len(cfg["tags"])
            cfg["tags"] = [t for t in cfg["tags"] if t.lower() != arg.lower()]
            if len(cfg["tags"]) < before:
                send_telegram(f"🗑 Removed tag filter: <code>{html.escape(arg)}</code>")
            else:
                send_telegram(f"Tag <code>{html.escape(arg)}</code> was not found in your filters.")

        elif cmd == "/cleartags":
            cfg["tags"] = []
            send_telegram("🧹 Cleared all tag filters. Now tracking all <code>good first issue</code> tickets.")

        elif cmd == "/stale":
            if arg.isdigit() and int(arg) > 0:
                cfg["stale_days"] = int(arg)
                send_telegram(f"⏳ Stale threshold updated to <b>{cfg['stale_days']} days</b>.")
            else:
                send_telegram("Usage: <code>/stale 14</code> (positive number of days)")

        elif cmd == "/pause":
            cfg["paused"] = True
            send_telegram("⏸ Notifications paused. Send <code>/resume</code> to re-enable.")

        elif cmd == "/resume":
            cfg["paused"] = False
            send_telegram("▶️ Notifications resumed.")

        elif cmd == "/list":
            matching = [
                i for i in issues
                if not i["assignees"]["nodes"] and matches_tags(i, cfg["tags"])
            ][:5]
            if not matching:
                send_telegram("No unassigned open issues match your current filters right now.")
            else:
                for issue in matching:
                    send_telegram(format_issue("📌 Open & Unassigned", issue))

        else:
            send_telegram("Unknown command. Send <code>/help</code> to see available commands.")


def main() -> None:
    issues = fetch_issues()
    state = load_state()

    # Always process incoming Telegram commands first
    process_commands(state, issues)

    cfg = state["config"]
    tracked = state["issues"]
    first_run = len(tracked) == 0
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=cfg["stale_days"])

    for issue in issues:
        num = str(issue["number"])
        assignees = sorted(a["login"] for a in issue["assignees"]["nodes"])

        linked_prs = {}
        for item in issue["timelineItems"]["nodes"]:
            src = item.get("source")
            if src and "number" in src:
                linked_prs[str(src["number"])] = src["state"]

        has_open_pr = any(st == "OPEN" for st in linked_prs.values())
        has_claim_comment = any(
            CLAIM_PATTERN.search(c["bodyText"]) for c in issue["comments"]["nodes"]
        )
        updated_at = datetime.fromisoformat(issue["updatedAt"].replace("Z", "+00:00"))
        prev = tracked.get(num)

        if first_run:
            tracked[num] = {
                "assignees": assignees,
                "prs": linked_prs,
                "notified_stale": False,
            }
            continue

        should_notify = (not cfg["paused"]) and matches_tags(issue, cfg["tags"])

        # 1. New issue
        if prev is None:
            if should_notify:
                send_telegram(format_issue("🆕 New Good First Issue", issue))
            tracked[num] = {
                "assignees": assignees,
                "prs": linked_prs,
                "notified_stale": False,
            }
            continue

        # 2a. Dropped: unassigned
        if prev["assignees"] and not assignees:
            if should_notify:
                send_telegram(
                    format_issue(
                        "🔄 Issue Unassigned (Dropped)",
                        issue,
                        f"Previously assigned to: {', '.join(prev['assignees'])}",
                    )
                )
            prev["notified_stale"] = False

        # 2b. Dropped: linked PR closed without merging
        for pr_num, pr_state in linked_prs.items():
            if pr_state == "CLOSED" and prev["prs"].get(pr_num) != "CLOSED":
                if should_notify:
                    send_telegram(
                        format_issue(
                            "🔄 Linked PR Closed Without Merging",
                            issue,
                            f"PR #{pr_num} was closed — issue may be free again.",
                        )
                    )

        # 2c. Stale
        is_claimed = bool(assignees) or has_claim_comment
        if (
            is_claimed
            and not has_open_pr
            and updated_at < stale_cutoff
            and not prev.get("notified_stale")
        ):
            if should_notify:
                days_idle = (now - updated_at).days
                send_telegram(
                    format_issue(
                        "⏳ Potentially Stale Issue",
                        issue,
                        f"No activity for {days_idle} days and no open PR. Consider pinging the claimant.",
                    )
                )
            prev["notified_stale"] = True

        prev["assignees"] = assignees
        prev["prs"] = linked_prs

    if first_run:
        send_telegram(
            f"✅ <b>LLVM Good First Issue Tracker initialized!</b>\n"
            f"Tracking {len(issues)} open issues. Send <code>/help</code> to see available commands."
        )

    save_state(state)


if __name__ == "__main__":
    main()
