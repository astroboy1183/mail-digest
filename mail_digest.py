#!/usr/bin/env python3
"""Mail digest.

One Telegram message every morning (~6:07 IST via GitHub Actions): ALL
Gmail from the previous 24h (6AM→6AM IST window) sorted into needs-action,
FYI and noise.

One agent, one task, one bot (@jayanth_morning_email_bot). Weather, news
and cricket are separate agents on their own bots.

Hard failures raise and land in the Actions log.
"""

import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from agentlib import ask_llm, send_telegram

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
IST = ZoneInfo("Asia/Kolkata")

# Substring-matched against the From header (case-insensitive). VIP mail
# must never land in NOISE, however boring the subject looks. Comes from
# the VIP_SENDERS secret (comma-separated), NOT code — this repo is
# public and personal addresses don't belong in it.
VIP_SENDERS = [
    s.strip() for s in os.environ.get("VIP_SENDERS", "").split(",") if s.strip()
]


def digest_window():
    """(start, end): the 24h window ending at the most recent 6:00 AM IST.

    Anchored — not rolling — so a delayed run still covers the same
    6AM→6AM day with no gaps or overlap.
    """
    now = datetime.now(IST)
    end = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now < end:
        end -= timedelta(days=1)
    return end - timedelta(days=1), end


def gmail_service():
    """Authenticated Gmail client; silent token refresh, browser only locally."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.environ.get("CI"):
                # In GitHub Actions there is no browser: fail loudly instead
                # of hanging. Fix: re-run the OAuth flow locally and update
                # the GMAIL_TOKEN_JSON repo secret.
                raise RuntimeError(
                    "token.json missing or unrefreshable in CI — refresh it "
                    "locally and update the GMAIL_TOKEN_JSON secret."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)
    return build("gmail", "v1", credentials=creds)


def fetch_emails(service, start, end):
    """Every message in [start, end) — read or unread — minus spam."""
    query = f"after:{int(start.timestamp())} before:{int(end.timestamp())} -category:spam"
    ids = []
    page_token = None
    while True:
        resp = (
            service.users()
            .messages()
            .list(userId="me", q=query, maxResults=100, pageToken=page_token)
            .execute()
        )
        ids.extend(m["id"] for m in resp.get("messages", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    emails = []
    for msg_id in ids:
        msg = (
            service.users()
            .messages()
            .get(
                userId="me",
                id=msg_id,
                format="metadata",
                metadataHeaders=["From", "Subject"],
            )
            .execute()
        )
        headers = {
            h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])
        }
        sender = headers.get("From", "(unknown sender)")
        emails.append(
            {
                "from": sender,
                "subject": headers.get("Subject", "(no subject)"),
                "snippet": msg.get("snippet", ""),
                # deep link straight to the message in the Gmail web UI
                "link": f"https://mail.google.com/mail/u/0/#all/{msg_id}",
                "vip": any(v.lower() in sender.lower() for v in VIP_SENDERS),
            }
        )
    return emails


def summarize(emails):
    """One model call: the day's mail sorted by what it needs from me."""
    email_lines = "\n".join(
        f"- From: {e['from']}{' [VIP]' if e['vip'] else ''} | "
        f"Subject: {e['subject']} | Snippet: {e['snippet']} | Link: {e['link']}"
        for e in emails
    )
    prompt = (
        "You are composing my morning mail digest. Be terse. "
        "Plain text only — no markdown headers or bold.\n\n"
        "=== INPUT: every email from the last 24h "
        "(sender, subject, snippet, link) ===\n"
        f"{email_lines}\n\n"
        "Produce EXACTLY this output structure:\n\n"
        "<one-line headline for the day's mail>\n"
        "NEEDS ACTION: emails needing a reply/decision/deadline — sender + "
        "what + when, then the email's Link on its own line (or 'nothing' "
        "if none). Copy links verbatim — never invent one.\n"
        "FYI: noteworthy, no action needed, grouped by sender/thread\n"
        "NOISE: one line — count of newsletters/promos/automated mail\n\n"
        "Emails marked [VIP] must appear under NEEDS ACTION or FYI, "
        "never NOISE."
    )
    return ask_llm(prompt)


def main():
    load_dotenv(BASE_DIR / ".env")
    start, end = digest_window()

    service = gmail_service()
    emails = fetch_emails(service, start, end)

    header = (
        f"📬 Mail digest — {end:%a %d %b %Y}\n"
        f"(window {start:%H:%M %d %b} → {end:%H:%M %d %b} IST, "
        f"{len(emails)} emails)\n\n"
    )
    body = summarize(emails) if emails else "Quiet inbox: no email in 24h ☕"
    send_telegram(header + body)


if __name__ == "__main__":
    main()
