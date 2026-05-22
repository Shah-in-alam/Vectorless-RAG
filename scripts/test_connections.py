"""Per-pipeline connection-test script.

Hits a no-op endpoint on every configured provider so the user can find
out about a broken / revoked secret BEFORE the next daily cron silently
drops a ticket. Run via workflow_dispatch on test-connection.yml from
the dashboard's "Test connection" button.

Each check returns one line:
  ✓ Provider: <detail>     -- success
  ✗ Provider: <reason>     -- failure (script exits 1 if any present)
  ○ Provider: not configured (skipped)

Exit code 0 = everything that was configured is healthy.
Exit code 1 = at least one failure -- check the line for details.
"""

from __future__ import annotations

import base64
import os
import sys

import httpx


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def test_jira() -> str:
    domain = _env("JIRA_DOMAIN")
    email = _env("JIRA_EMAIL")
    token = _env("JIRA_API_TOKEN")
    project = _env("JIRA_PROJECT_KEY")
    if not all([domain, email, token, project]):
        return "✗ Jira: one or more secrets unset (JIRA_DOMAIN / JIRA_EMAIL / JIRA_API_TOKEN / JIRA_PROJECT_KEY)"
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}", "Accept": "application/json"}
    try:
        r = httpx.get(f"https://{domain}/rest/api/3/myself", headers=headers, timeout=15)
        if r.status_code != 200:
            return f"✗ Jira /myself: HTTP {r.status_code} ({r.text[:100]})"
        me = r.json()
        pr = httpx.get(
            f"https://{domain}/rest/api/3/project/{project}",
            headers=headers,
            timeout=15,
        )
        if pr.status_code != 200:
            return f"✗ Jira project {project}: HTTP {pr.status_code} (token works but project not accessible)"
        return f"✓ Jira: authed as {me.get('emailAddress', '?')}, project {project} accessible"
    except Exception as e:
        return f"✗ Jira: {e}"


def test_gmail() -> str:
    email = _env("GMAIL_EMAIL")
    pw = _env("GMAIL_APP_PASSWORD")
    label = _env("GMAIL_LABEL") or "TaskFlow"
    if not all([email, pw]):
        return "✗ Gmail: GMAIL_EMAIL or GMAIL_APP_PASSWORD unset"
    try:
        from imap_tools import MailBox

        with MailBox("imap.gmail.com").login(email, pw, label) as mb:
            # Confirm the label folder is selectable
            _ = list(mb.fetch(limit=1, mark_seen=False))
        return f"✓ Gmail: {email}, label '{label}' accessible"
    except Exception as e:
        return f"✗ Gmail: {e}"


def test_github() -> str:
    token = _env("GH_TOKEN")
    repo = _env("GH_REPO")
    if not all([token, repo]):
        return "✗ GitHub: GH_TOKEN or GH_REPO unset"
    try:
        r = httpx.get(
            f"https://api.github.com/repos/{repo}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        if r.status_code != 200:
            return f"✗ GitHub repo {repo}: HTTP {r.status_code} ({r.text[:100]})"
        return f"✓ GitHub: repo {repo} accessible (built-in workflow token)"
    except Exception as e:
        return f"✗ GitHub: {e}"


def test_slack() -> str:
    url = _env("SLACK_WEBHOOK_URL")
    if not url:
        return "○ Slack: SLACK_WEBHOOK_URL not set (skipped)"
    try:
        r = httpx.post(
            url,
            json={"text": "🧪 TaskFlow connection test — please ignore."},
            timeout=10,
        )
        if r.status_code != 200 or r.text.strip() != "ok":
            return f"✗ Slack: HTTP {r.status_code} ({r.text[:80]})"
        return "✓ Slack: webhook reachable (a 'connection test' message was posted)"
    except Exception as e:
        return f"✗ Slack: {e}"


def test_ai() -> str:
    provider = _env("AI_PROVIDER").lower()
    key = _env("AI_API_KEY")
    if not provider or not key:
        return "○ AI: AI_PROVIDER + AI_API_KEY not set (skipped)"
    try:
        if provider == "claude":
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "say ok"}],
                },
                timeout=20,
            )
        elif provider == "openai":
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "gpt-4o-mini",
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "say ok"}],
                },
                timeout=20,
            )
        elif provider == "gemini":
            r = httpx.get(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash?key={key}",
                timeout=15,
            )
        else:
            return f"✗ AI: unknown provider '{provider}' (expected claude/openai/gemini)"
        if r.status_code != 200:
            return f"✗ AI ({provider}): HTTP {r.status_code} ({r.text[:120]})"
        return f"✓ AI ({provider}): API key valid"
    except Exception as e:
        return f"✗ AI ({provider}): {e}"


def main() -> int:
    print("=" * 60)
    print(" TaskFlow connection test")
    print("=" * 60)
    results = [
        test_jira(),
        test_github(),
        test_gmail(),
        test_slack(),
        test_ai(),
    ]
    for line in results:
        print(line)
    print()
    fails = sum(1 for r in results if r.startswith("✗"))
    skips = sum(1 for r in results if r.startswith("○"))
    oks = sum(1 for r in results if r.startswith("✓"))
    print(f"Summary: ✓ {oks}   ○ {skips} skipped   ✗ {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
