"""Poll a Gmail inbox and create a Jira issue for every unread email.

Designed to run from GitHub Actions on a schedule. Idempotent: emails
are marked READ after a Jira issue is created, so re-running the script
will not double-create.

Only processes emails inside the Gmail label specified by GMAIL_LABEL
(default "TaskFlow"). Apply that label to an email in Gmail -- manually,
or via a Gmail filter rule -- to opt it in to ingestion. The INBOX is
never read directly.

Env vars (set as repo secrets in GitHub Actions):
  GMAIL_EMAIL, GMAIL_APP_PASSWORD       Gmail IMAP credentials
  GMAIL_LABEL (optional, default "TaskFlow")
  JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN, JIRA_PROJECT_KEY
  MAX_EMAILS (optional, default 20)     Hard cap per run, prevents runaway
"""

from __future__ import annotations

import base64
import os
import sys
from typing import Any

import httpx
from imap_tools import AND, MailBox


def _required(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise SystemExit(f"FATAL: env var {name} is empty or unset")
    return val


GMAIL_EMAIL = _required("GMAIL_EMAIL")
GMAIL_APP_PASSWORD = _required("GMAIL_APP_PASSWORD")
JIRA_DOMAIN = _required("JIRA_DOMAIN")
JIRA_EMAIL = _required("JIRA_EMAIL")
JIRA_API_TOKEN = _required("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.environ.get("JIRA_PROJECT_KEY", "SCRUM").strip() or "SCRUM"
MAX_EMAILS = int(os.environ.get("MAX_EMAILS", "20"))
GMAIL_LABEL = os.environ.get("GMAIL_LABEL", "TaskFlow").strip() or "TaskFlow"

JIRA_BASE = f"https://{JIRA_DOMAIN}"
_basic = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
JIRA_HEADERS = {
    "Authorization": f"Basic {_basic}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def _adf(text: str) -> dict[str, Any]:
    """Wrap plain text as Atlassian Document Format (Jira's description schema)."""
    paragraphs = text.split("\n\n") if text else [""]
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": p}] if p else [],
            }
            for p in paragraphs
        ],
    }


def find_active_sprint_id() -> int | None:
    """Return the id of the active sprint on the project's first Scrum board.

    Returns None if there is no Scrum board or no active sprint -- new tickets
    then simply sit in the Backlog without raising an error.
    """
    r = httpx.get(
        f"{JIRA_BASE}/rest/agile/1.0/board",
        params={"projectKeyOrId": JIRA_PROJECT_KEY},
        headers=JIRA_HEADERS,
        timeout=20,
    )
    if r.status_code >= 300:
        return None
    boards = r.json().get("values", [])
    for b in boards:
        bid = b["id"]
        sr = httpx.get(
            f"{JIRA_BASE}/rest/agile/1.0/board/{bid}/sprint",
            params={"state": "active"},
            headers=JIRA_HEADERS,
            timeout=20,
        )
        if sr.status_code >= 300:
            continue
        sprints = sr.json().get("values", [])
        if sprints:
            return sprints[0]["id"]
    return None


def add_to_sprint(jira_key: str, sprint_id: int) -> bool:
    r = httpx.post(
        f"{JIRA_BASE}/rest/agile/1.0/sprint/{sprint_id}/issue",
        json={"issues": [jira_key]},
        headers=JIRA_HEADERS,
        timeout=20,
    )
    return r.status_code < 300


def create_jira_issue(summary: str, body: str, sender: str) -> dict[str, Any]:
    description = (
        f"From: {sender}\n\n"
        f"---\n\n"
        f"{body or '(empty body)'}"
    )
    payload = {
        "fields": {
            "project": {"key": JIRA_PROJECT_KEY},
            "summary": summary[:255],
            "issuetype": {"name": "Task"},
            "description": _adf(description),
        }
    }
    r = httpx.post(
        f"{JIRA_BASE}/rest/api/3/issue",
        json=payload,
        headers=JIRA_HEADERS,
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"Jira create failed {r.status_code}: {r.text[:500]}")
    return r.json()


def main() -> int:
    print(
        f"Config: gmail={GMAIL_EMAIL}  label={GMAIL_LABEL}  jira={JIRA_BASE}  "
        f"project={JIRA_PROJECT_KEY}  max_emails={MAX_EMAILS}"
    )
    created = 0
    skipped = 0
    sprint_id = find_active_sprint_id()
    if sprint_id:
        print(f"Active sprint id={sprint_id} -- new tickets will land on the board")
    else:
        print("No active sprint found -- new tickets will sit in the Backlog")
    # Gmail exposes labels as IMAP folders. The "TaskFlow" label is at
    # the top-level mailbox of the same name. If you nest the label under
    # another, use the full IMAP path (e.g. "Parent/TaskFlow").
    with MailBox("imap.gmail.com").login(GMAIL_EMAIL, GMAIL_APP_PASSWORD, GMAIL_LABEL) as mailbox:
        unseen = list(mailbox.fetch(AND(seen=False), limit=MAX_EMAILS, mark_seen=False))
        print(f"Found {len(unseen)} unread email(s) in label '{GMAIL_LABEL}'")
        for msg in unseen:
            subject = (msg.subject or "(no subject)").strip()
            body = (msg.text or msg.html or "").strip()
            sender = msg.from_ or "(unknown sender)"
            try:
                issue = create_jira_issue(subject, body, sender)
                key = issue.get("key", "?")
                if sprint_id and add_to_sprint(key, sprint_id):
                    print(f"  CREATED {key} -> Sprint  <- '{subject[:60]}' from {sender}")
                else:
                    print(f"  CREATED {key} (Backlog) <- '{subject[:60]}' from {sender}")
                mailbox.flag(msg.uid, "\\Seen", True)
                created += 1
            except Exception as e:
                print(f"  FAILED  '{subject[:60]}'  -> {e}")
                skipped += 1
    print(f"Done. created={created} skipped={skipped} total_seen={len(unseen)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
