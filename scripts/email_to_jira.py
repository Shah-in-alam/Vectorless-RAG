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

  AI_PROVIDER (optional, "claude" | "openai" | "gemini")
  AI_API_KEY (required when AI_PROVIDER is set)
    When configured, the raw email is sent to the LLM to produce a clean
    Jira summary + description + issue type + priority. On any failure
    (rate limit, bad key, malformed JSON) the script silently falls back
    to using the raw subject/body so a broken AI key never blocks
    ingestion.
"""

from __future__ import annotations

import base64
import os
import re
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
AI_PROVIDER = os.environ.get("AI_PROVIDER", "").strip().lower()
AI_API_KEY = os.environ.get("AI_API_KEY", "").strip()

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


# --- Regex-based extraction (free, always on) ---
_SUBJECT_PREFIX_RE = re.compile(r"^(\s*(re|fw|fwd):\s*)+", re.I)
_SUBJECT_TAG_RE = re.compile(r"^\s*\[([A-Za-z]+)\]\s*")

# Strip quoted replies introduced by either "On <date> ... wrote:" or by a
# block of lines starting with ">", and any lines after "-- " (sig sentinel)
# or "Sent from my ..." (mobile sig).
_QUOTED_REPLY_RE = re.compile(r"\n+\s*On\s.+?wrote:.*$", re.S)
_QUOTED_LINES_RE = re.compile(r"(?:\n>.*)+$", re.M)
_SIGNATURE_RE = re.compile(r"\n--\s*\n.*$", re.S)
_MOBILE_SIG_RE = re.compile(r"\nSent from my .+$", re.M)
_DISCLAIMER_RE = re.compile(
    r"\n+(this email|the information contained|confidentiality notice)[^\n]*\n.*$",
    re.S | re.I,
)
# Collapse any 3+ blank lines to a max of 2
_BLANK_LINES_RE = re.compile(r"\n{3,}")

_BUG_HINTS = re.compile(
    r"\b(bug|broken|crash(?:ing|ed)?|error|exception|stack ?trace|not work|doesn'?t work|fails?|failure)\b",
    re.I,
)
_STORY_HINTS = re.compile(
    r"\b(feature|enhancement|add (?:a|the|support)|please add|implement|wish ?list|would like|request to add|new (?:page|feature|button|option))\b",
    re.I,
)
_HIGH_PRI_RE = re.compile(
    r"\b(urgent|asap|critical|blocker|blocking|p0|p1|on fire|right now|outage)\b",
    re.I,
)
_LOW_PRI_RE = re.compile(
    r"\b(low ?priority|when (?:you have time|convenient)|whenever|nice ?to ?have|not urgent)\b",
    re.I,
)


def regex_extract(subject: str, body: str) -> dict[str, Any]:
    """Always-on cleanup of an email into Jira-ready fields. No external calls."""
    # ---- Clean subject ----
    s = (subject or "").strip()
    s = _SUBJECT_PREFIX_RE.sub("", s)  # strip Re:/Fw:/Fwd:

    issue_type = "Task"
    m = _SUBJECT_TAG_RE.match(s)
    if m:
        tag = m.group(1).upper()
        if tag in ("BUG", "ISSUE", "ERROR"):
            issue_type = "Bug"
            s = _SUBJECT_TAG_RE.sub("", s).strip()
        elif tag in ("FEATURE", "STORY", "REQUEST"):
            issue_type = "Story"
            s = _SUBJECT_TAG_RE.sub("", s).strip()
        elif tag == "TASK":
            s = _SUBJECT_TAG_RE.sub("", s).strip()
        # other tags (e.g. [URGENT]) -- keep them visible in the title

    if len(s) > 120:
        s = s[:117].rstrip() + "..."

    # ---- Clean body ----
    b = (body or "").strip()
    b = _QUOTED_REPLY_RE.sub("", b)
    b = _QUOTED_LINES_RE.sub("", b)
    b = _SIGNATURE_RE.sub("", b)
    b = _MOBILE_SIG_RE.sub("", b)
    b = _DISCLAIMER_RE.sub("", b)
    b = _BLANK_LINES_RE.sub("\n\n", b).strip()

    # ---- Detect issue_type from content (only if tag didn't already set it) ----
    haystack = f"{subject}\n{body}"
    if issue_type == "Task":
        if _BUG_HINTS.search(haystack):
            issue_type = "Bug"
        elif _STORY_HINTS.search(haystack):
            issue_type = "Story"

    # ---- Detect priority ----
    if _HIGH_PRI_RE.search(haystack):
        priority = "High"
    elif _LOW_PRI_RE.search(haystack):
        priority = "Low"
    else:
        priority = "Medium"

    return {
        "summary": s or (subject or "(no subject)"),
        "description": b or (body or "(empty body)"),
        "issue_type": issue_type,
        "priority": priority,
    }


AI_SYSTEM_PROMPT = (
    "You convert a raw email into a structured Jira ticket. "
    "Reply ONLY with a JSON object matching this schema (no prose, no fences):\n"
    '{"summary": string (<= 100 chars, no quotes), '
    '"description": string (clean, multi-line ok), '
    '"issue_type": "Task" | "Bug" | "Story", '
    '"priority": "Highest" | "High" | "Medium" | "Low" | "Lowest"}\n'
    "Rules: strip greetings, signatures, disclaimers, quoted prior replies, "
    "and tracking footers. Detect the underlying intent and pick issue_type "
    "(Bug = something broken; Story = new feature/work request; Task = "
    "everything else). Pick priority from urgency cues in the text "
    '(absent -> "Medium"). Keep description faithful to the email body.'
)


def _parse_ai_json(text: str) -> dict[str, Any] | None:
    """Tolerant JSON extraction: handles fenced or unfenced responses."""
    import json
    import re

    # Strip ```json ... ``` or ``` ... ``` fences if present.
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else text
    # Fall back to the first { ... } block.
    if not candidate.lstrip().startswith("{"):
        m2 = re.search(r"\{.*\}", candidate, re.S)
        if not m2:
            return None
        candidate = m2.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def extract_with_ai(subject: str, body: str, sender: str) -> dict[str, Any] | None:
    """Call the configured LLM. Returns enriched fields or None on any failure."""
    if not AI_PROVIDER or not AI_API_KEY:
        return None
    user_msg = (
        f"From: {sender}\nSubject: {subject}\n\nBody:\n{body[:6000]}"
    )
    try:
        if AI_PROVIDER == "claude":
            r = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": AI_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5",
                    "max_tokens": 1024,
                    "system": AI_SYSTEM_PROMPT,
                    "messages": [{"role": "user", "content": user_msg}],
                },
                timeout=30,
            )
            r.raise_for_status()
            text = r.json()["content"][0]["text"]
        elif AI_PROVIDER == "openai":
            r = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [
                        {"role": "system", "content": AI_SYSTEM_PROMPT},
                        {"role": "user", "content": user_msg},
                    ],
                    "response_format": {"type": "json_object"},
                },
                timeout=30,
            )
            r.raise_for_status()
            text = r.json()["choices"][0]["message"]["content"]
        elif AI_PROVIDER == "gemini":
            r = httpx.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={AI_API_KEY}",
                json={
                    "systemInstruction": {"parts": [{"text": AI_SYSTEM_PROMPT}]},
                    "contents": [{"parts": [{"text": user_msg}]}],
                    "generationConfig": {"responseMimeType": "application/json"},
                },
                timeout=30,
            )
            r.raise_for_status()
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        else:
            return None
    except Exception as e:
        print(f"    AI ({AI_PROVIDER}) failed -- using raw email. {e}")
        return None

    parsed = _parse_ai_json(text)
    if not parsed or "summary" not in parsed:
        print(f"    AI ({AI_PROVIDER}) returned unparseable output -- using raw")
        return None
    return parsed


def create_jira_issue(
    summary: str,
    body: str,
    sender: str,
    issue_type: str = "Task",
    priority: str | None = None,
) -> dict[str, Any]:
    description = (
        f"From: {sender}\n\n"
        f"---\n\n"
        f"{body or '(empty body)'}"
    )
    fields: dict[str, Any] = {
        "project": {"key": JIRA_PROJECT_KEY},
        "summary": summary[:255],
        "issuetype": {"name": issue_type},
        "description": _adf(description),
    }
    if priority:
        fields["priority"] = {"name": priority}
    r = httpx.post(
        f"{JIRA_BASE}/rest/api/3/issue",
        json={"fields": fields},
        headers=JIRA_HEADERS,
        timeout=30,
    )
    # If priority field isn't on the screen, Jira returns 400 -- retry without.
    if r.status_code == 400 and "priority" in r.text.lower():
        fields.pop("priority", None)
        r = httpx.post(
            f"{JIRA_BASE}/rest/api/3/issue",
            json={"fields": fields},
            headers=JIRA_HEADERS,
            timeout=30,
        )
    # If issue_type doesn't exist in the project, fall back to Task.
    if r.status_code == 400 and "issuetype" in r.text.lower():
        fields["issuetype"] = {"name": "Task"}
        r = httpx.post(
            f"{JIRA_BASE}/rest/api/3/issue",
            json={"fields": fields},
            headers=JIRA_HEADERS,
            timeout=30,
        )
    if r.status_code >= 300:
        raise RuntimeError(f"Jira create failed {r.status_code}: {r.text[:500]}")
    return r.json()


def main() -> int:
    ai_status = f"on ({AI_PROVIDER})" if AI_PROVIDER and AI_API_KEY else "off"
    print(
        f"Config: gmail={GMAIL_EMAIL}  label={GMAIL_LABEL}  jira={JIRA_BASE}  "
        f"project={JIRA_PROJECT_KEY}  max_emails={MAX_EMAILS}  ai={ai_status}"
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
            # Step 1: always-on regex cleanup (free, deterministic).
            rx = regex_extract(subject, body)
            summary, description = rx["summary"], rx["description"]
            issue_type, priority = rx["issue_type"], rx["priority"]
            print(
                f"    regex -> type={issue_type} priority={priority} "
                f"title='{summary[:50]}'"
            )
            # Step 2: optional AI overlay -- only if AI_PROVIDER + key set
            # AND the call succeeds. Overrides regex on success.
            ai = extract_with_ai(subject, body, sender)
            if ai:
                summary = ai.get("summary") or summary
                description = ai.get("description") or description
                issue_type = ai.get("issue_type") or issue_type
                priority = ai.get("priority") or priority
                print(
                    f"    AI ({AI_PROVIDER}) overlay -> type={issue_type} "
                    f"priority={priority}"
                )
            try:
                issue = create_jira_issue(
                    summary, description, sender, issue_type=issue_type, priority=priority
                )
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
