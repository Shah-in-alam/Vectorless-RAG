"""Poll Jira and mirror new issues into GitHub.

Designed to run from GitHub Actions on a schedule (every 5 min).
Idempotent: GitHub itself is the dedup source. A Jira issue is mirrored
only if no GitHub issue (open or closed) has title prefix "<KEY>:".

Env vars (set as repo secrets in GitHub Actions):
  JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
  GH_TOKEN   (the built-in GITHUB_TOKEN works)
  GH_REPO    (e.g. "Zetorai/TaskFlow-AI-")
  LOOKBACK_HOURS (optional, default 1)
"""

from __future__ import annotations

import base64
import os
import re
import sys
from typing import Any

import httpx

def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"FATAL: env var {name} is empty or unset")
    return val


JIRA_DOMAIN = _required("JIRA_DOMAIN")
JIRA_EMAIL = _required("JIRA_EMAIL")
JIRA_API_TOKEN = _required("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SCRUM").strip() or "SCRUM"
GH_TOKEN = _required("GH_TOKEN")
GH_REPO = _required("GH_REPO")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "1"))
AUTO_BRANCH = os.environ.get("AUTO_BRANCH", "1").lower() not in ("0", "false", "no")
BASE_BRANCH = os.environ.get("BASE_BRANCH", "main").strip() or "main"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()

if JIRA_DOMAIN.startswith(("http://", "https://")):
    raise SystemExit(
        "FATAL: JIRA_DOMAIN must NOT include a scheme. "
        "Use e.g. 'shahinmo.atlassian.net', not 'https://shahinmo.atlassian.net'."
    )
if "/" in JIRA_DOMAIN:
    raise SystemExit(
        "FATAL: JIRA_DOMAIN must be just the host. Got something containing '/'."
    )

JIRA_BASE = f"https://{JIRA_DOMAIN}"
GH_BASE = f"https://api.github.com/repos/{GH_REPO}"

print(f"Config: JIRA_BASE={JIRA_BASE}  project={JIRA_PROJECT_KEY}  "
      f"lookback={LOOKBACK_HOURS}h  github_repo={GH_REPO}")

_basic = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
JIRA_HEADERS = {
    "Authorization": f"Basic {_basic}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _plain(adf: Any) -> str:
    if not isinstance(adf, dict):
        return str(adf or "")
    out: list[str] = []
    for block in adf.get("content", []):
        for node in block.get("content", []) or []:
            if node.get("type") == "text":
                out.append(node.get("text", ""))
        out.append("\n")
    return "".join(out).strip()


def jira_recent_issues() -> list[dict[str, Any]]:
    jql = (
        f"project = {JIRA_PROJECT_KEY} "
        f"AND created >= -{LOOKBACK_HOURS}h ORDER BY created DESC"
    )
    r = httpx.post(
        f"{JIRA_BASE}/rest/api/3/search/jql",
        json={
            "jql": jql,
            "fields": [
                "summary",
                "description",
                "status",
                "issuetype",
                "priority",
                "reporter",
            ],
        },
        headers=JIRA_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("issues", [])


def list_mirrored_jira_keys() -> set[str]:
    """Return all Jira keys already mirrored to GitHub.

    Reads GitHub issues filtered by the `from-jira` label and parses the
    title prefix. Uses the issues endpoint (not search/issues) because
    GitHub search tokenizes punctuation and gives false positives for
    keys whose digits overlap (e.g. "SCRUM-10:" matched "SCRUM-8 ...10 am...").
    """
    keys: set[str] = set()
    page = 1
    while True:
        r = httpx.get(
            f"{GH_BASE}/issues",
            params={
                "labels": "from-jira",
                "state": "all",
                "per_page": 100,
                "page": page,
            },
            headers=GH_HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        for item in batch:
            if "pull_request" in item:
                continue
            title = item.get("title", "")
            if ":" in title:
                key = title.split(":", 1)[0].strip()
                if key:
                    keys.add(key)
        if len(batch) < 100:
            break
        page += 1
    return keys


def notify_slack(text: str, link_url: str | None = None) -> None:
    """Fire-and-forget Slack message via Incoming Webhook. Silent on failure."""
    if not SLACK_WEBHOOK_URL:
        return
    payload: dict[str, Any] = {"text": text}
    if link_url:
        payload["blocks"] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open"},
                    "url": link_url,
                },
            }
        ]
    try:
        httpx.post(SLACK_WEBHOOK_URL, json=payload, timeout=10)
    except Exception:
        pass


def _slug(text: str, max_len: int = 40) -> str:
    """Convert summary into a git-safe slug: lowercase, dashes for runs of non-alnum."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return s[:max_len].rstrip("-") or "task"


def create_github_branch(jira_key: str, summary: str) -> str | None:
    """Create a branch named <KEY>-<slug> from BASE_BRANCH.

    Returns the branch name on success, None on failure (already-exists is
    treated as a success because re-running shouldn't error).
    """
    branch = f"{jira_key}-{_slug(summary)}"
    # Get HEAD sha of base branch
    r = httpx.get(
        f"{GH_BASE}/git/refs/heads/{BASE_BRANCH}",
        headers=GH_HEADERS,
        timeout=20,
    )
    if r.status_code != 200:
        print(f"    branch: cannot read base {BASE_BRANCH} ({r.status_code})")
        return None
    sha = r.json()["object"]["sha"]
    # Create new ref
    r = httpx.post(
        f"{GH_BASE}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": sha},
        headers=GH_HEADERS,
        timeout=20,
    )
    if r.status_code == 201:
        return branch
    if r.status_code == 422 and "already exists" in r.text.lower():
        return branch  # idempotent: re-runs see existing branch as success
    print(f"    branch: create failed ({r.status_code}): {r.text[:160]}")
    return None


def create_github_issue(jira_issue: dict[str, Any]) -> dict[str, Any]:
    f = jira_issue["fields"]
    key = jira_issue["key"]
    summary = f["summary"]
    desc = _plain(f.get("description"))
    reporter = (f.get("reporter") or {}).get("displayName") or "(unknown)"
    priority = (f.get("priority") or {}).get("name") or "(unset)"
    itype = (f.get("issuetype") or {}).get("name") or "Task"
    url = f"{JIRA_BASE}/browse/{key}"
    body = (
        f"Auto-created from Jira issue [{key}]({url}).\n\n"
        f"**Description:**\n{desc or '_(empty)_'}\n\n"
        f"**Reporter:** {reporter}\n"
        f"**Priority:** {priority}\n"
        f"**Type:** {itype}"
    )
    r = httpx.post(
        f"{GH_BASE}/issues",
        json={"title": f"{key}: {summary}", "body": body, "labels": ["from-jira"]},
        headers=GH_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def main() -> int:
    issues = jira_recent_issues()
    if not issues:
        print(f"No Jira issues created in the last {LOOKBACK_HOURS}h.")
        return 0
    mirrored = list_mirrored_jira_keys()
    print(f"Already mirrored in GitHub: {len(mirrored)} keys")
    created = skipped = 0
    for issue in issues:
        key = issue["key"]
        if key in mirrored:
            print(f"  skip {key} (already in GitHub)")
            skipped += 1
            continue
        gh = create_github_issue(issue)
        print(f"  CREATED {key} -> {gh['html_url']}")
        branch = None
        if AUTO_BRANCH:
            branch = create_github_branch(key, issue["fields"]["summary"])
            if branch:
                print(f"    branch: {branch}")
        notify_slack(
            f"🐙 *{key}* mirrored to GitHub: <{gh['html_url']}|#{gh['number']}>"
            + (f"\n_branch: `{branch}`_" if branch else ""),
            link_url=gh["html_url"],
        )
        created += 1
    print(f"Done. created={created} skipped={skipped} total_seen={len(issues)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
