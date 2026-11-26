import html
import json
import os
import re
from datetime import datetime, timezone, timedelta
import requests

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "state.json"
STALE_DAYS = 14  # Flag assigned/claimed issues with no activity for 14+ days

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
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def main() -> None:
    issues = fetch_issues()
    state = load_state()
    first_run = len(state) == 0
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(days=STALE_DAYS)

    for issue in issues:
        num = str(issue["number"])
        assignees = sorted(a["login"] for a in issue["assignees"]["nodes"])

        # Extract linked PRs from timeline
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

        prev = state.get(num)

        if first_run:
            # Initialize state silently so you don't get spammed with 30 old issues
            state[num] = {
                "assignees": assignees,
                "prs": linked_prs,
                "notified_stale": False,
            }
            continue

        # 1. New issue
        if prev is None:
            send_telegram(format_issue("🆕 New Good First Issue", issue))
            state[num] = {
                "assignees": assignees,
                "prs": linked_prs,
                "notified_stale": False,
            }
            continue

        # 2a. Dropped: previously assigned, now unassigned
        if prev["assignees"] and not assignees:
            send_telegram(
                format_issue(
                    "🔄 Issue Unassigned (Dropped)",
                    issue,
                    f"Previously assigned to: {', '.join(prev['assignees'])}",
                )
            )
            prev["notified_stale"] = False

        # 2b. Dropped: a linked PR was closed without merging
        for pr_num, pr_state in linked_prs.items():
            if pr_state == "CLOSED" and prev["prs"].get(pr_num) != "CLOSED":
                send_telegram(
                    format_issue(
                        "🔄 Linked PR Closed Without Merging",
                        issue,
                        f"PR #{pr_num} was closed — issue may be free again.",
                    )
                )

        # 2c. Stale: assigned or claimed, no open PR, inactive for STALE_DAYS+
        is_claimed = bool(assignees) or has_claim_comment
        if (
            is_claimed
            and not has_open_pr
            and updated_at < stale_cutoff
            and not prev.get("notified_stale")
        ):
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
            f"Tracking {len(issues)} open issues. You'll be notified on new, dropped, or stale ({STALE_DAYS}+ days) issues."
        )

    save_state(state)


if __name__ == "__main__":
    main()
