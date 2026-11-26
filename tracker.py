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
STALE_DAYS = 7
MAX_PAGES = 15  # Up to 750 open good first issues

CLAIM_RE = re.compile(
    r"\b(can i work on|i('d| would) like to work|work(ing)? on this|assign( me)?|"
    r"i('ll| will) take|claim(ing)? this|sure,? go ahead|all yours)\b",
    re.IGNORECASE,
)

DROP_RE = re.compile(
    r"\b(can'?t work on|unable to work|no longer working|dropping this|"
    r"unassign me|feel free to take|someone else can take)\b",
    re.IGNORECASE,
)

GRAPHQL_QUERY = """
query($cursor: String) {
  repository(owner: "llvm", name: "llvm-project") {
    issues(
      first: 50
      after: $cursor
      states: OPEN
      labels: ["good first issue"]
      orderBy: { field: CREATED_AT, direction: DESC }
    ) {
      pageInfo {
        hasNextPage
        endCursor
      }
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
        comments(last: 15) {
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


def format_issue(badge: str, info: dict, extra_note: str = "") -> str:
    title = html.escape(info["title"])
    tags_str = ", ".join(f"<code>{html.escape(l)}</code>" for l in info["labels"]) or "None"
    lines = [
        f"<b>{badge}</b>",
        f"<b>#{info['number']}:</b> {title}",
        f"<b>Opened:</b> {info['created_at'][:10]}",
        f"<b>Tags:</b> {tags_str}",
    ]
    if extra_note:
        lines.append(f"<b>Note:</b> {html.escape(extra_note)}")
    lines.append(f"<a href=\"{info['url']}\">Open on GitHub</a>")
    return "\n".join(lines)


def fetch_all_issues() -> list[dict]:
    all_issues = []
    cursor = None
    for _ in range(MAX_PAGES):
        resp = requests.post(
            "https://api.github.com/graphql",
            json={"query": GRAPHQL_QUERY, "variables": {"cursor": cursor}},
            headers={"Authorization": f"Bearer {GITHUB_TOKEN}"},
            timeout=25,
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(f"GitHub GraphQL error: {data['errors']}")

        issues_block = data["data"]["repository"]["issues"]
        all_issues.extend(issues_block["nodes"])

        if not issues_block["pageInfo"]["hasNextPage"]:
            break
        cursor = issues_block["pageInfo"]["endCursor"]
    return all_issues


def analyze_issue(issue: dict, now: datetime) -> dict:
    assignees = sorted(a["login"] for a in issue["assignees"]["nodes"])
    labels = [lbl["name"] for lbl in issue["labels"]["nodes"] if lbl["name"] != "good first issue"]

    linked_prs = {}
    for item in issue["timelineItems"]["nodes"]:
        src = item.get("source")
        if src and "number" in src:
            linked_prs[str(src["number"])] = src["state"]

    has_open_pr = any(st == "OPEN" for st in linked_prs.values())

    # Scan comments chronologically to detect claims and explicit drops
    comment_claimed_by = None
    last_comment_at = None
    for c in issue["comments"]["nodes"]:
        body = c["bodyText"] or ""
        author = (c.get("author") or {}).get("login", "unknown")
        last_comment_at = c["createdAt"]
        if DROP_RE.search(body):
            comment_claimed_by = None
        elif CLAIM_RE.search(body):
            comment_claimed_by = author

    last_activity_str = last_comment_at or issue["updatedAt"]
    last_activity = datetime.fromisoformat(last_activity_str.replace("Z", "+00:00"))
    days_idle = (now - last_activity).days

    is_claimed = bool(assignees) or (comment_claimed_by is not None)

    if has_open_pr:
        status = "in_progress"
    elif not is_claimed:
        status = "unclaimed"
    elif days_idle >= STALE_DAYS:
        status = "stale"
    else:
        status = "active"

    return {
        "number": issue["number"],
        "title": issue["title"],
        "url": issue["url"],
        "created_at": issue["createdAt"],
        "updated_at": issue["updatedAt"],
        "labels": labels,
        "assignees": assignees,
        "comment_claimed_by": comment_claimed_by,
        "prs": linked_prs,
        "days_idle": days_idle,
        "status": status,  # unclaimed | active | stale | in_progress
    }


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_scan": None, "issues": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def main() -> None:
    now = datetime.now(timezone.utc)
    raw_issues = fetch_all_issues()
    state = load_state()
    prev_issues = state.get("issues", {})
    first_run = len(prev_issues) == 0

    new_issues_map = {}

    for raw in raw_issues:
        info = analyze_issue(raw, now)
        num = str(info["number"])
        prev = prev_issues.get(num)

        # Preserve notification flags
        info["notified_stale"] = prev.get("notified_stale", False) if prev else False

        if not first_run:
            # 1. Brand-new issue
            if prev is None:
                send_telegram(format_issue("🆕 New Good First Issue", info))

            else:
                # 2. Previously claimed/assigned, now unclaimed (dropped)
                if prev.get("status") in ("active", "stale", "in_progress") and info["status"] == "unclaimed":
                    send_telegram(
                        format_issue(
                            "🔄 Issue Dropped / Unclaimed",
                            info,
                            "Previously had an assignee/claimant or PR, now appears free.",
                        )
                    )
                    info["notified_stale"] = False

                # 3. Newly stale (> 7 days idle, no open PR)
                elif info["status"] == "stale" and not info["notified_stale"]:
                    who = ", ".join(info["assignees"]) or info["comment_claimed_by"] or "someone"
                    send_telegram(
                        format_issue(
                            "⏳ Stale Issue (>7 Days Idle)",
                            info,
                            f"Claimed by {who}, idle for {info['days_idle']} days with no open PR.",
                        )
                    )
                    info["notified_stale"] = True

        new_issues_map[num] = info

    if first_run:
        unclaimed_cnt = sum(1 for i in new_issues_map.values() if i["status"] == "unclaimed")
        stale_cnt = sum(1 for i in new_issues_map.values() if i["status"] == "stale")
        send_telegram(
            f"✅ <b>LLVM Tracker v2 Initialized!</b>\n"
            f"Scanned <b>{len(new_issues_map)}</b> total open issues.\n"
            f"• Unclaimed: <b>{unclaimed_cnt}</b>\n"
            f"• Stale (&gt;{STALE_DAYS}d, no PR): <b>{stale_cnt}</b>\n\n"
            f"Send <code>/help</code> for instant commands."
        )

    save_state({"last_scan": now.strftime("%Y-%m-%d %H:%M UTC"), "issues": new_issues_map})


if __name__ == "__main__":
    main()
