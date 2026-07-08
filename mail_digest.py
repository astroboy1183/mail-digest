#!/usr/bin/env python3
"""Mail digest.

One Telegram message every morning (~6:07 IST via GitHub Actions): ALL
Gmail from the previous 24h (6AM→6AM IST window) sorted into needs-action,
FYI and noise.

One agent, one task, one bot (@jayanth_morning_email_bot). Weather, news
and cricket are separate agents on their own bots.

Hard failures raise and land in the Actions log.
"""

import json
import os
import re
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
                # thread this message belongs to — messages in the same
                # conversation are collapsed to one digest entry downstream
                "threadId": msg.get("threadId"),
            }
        )
    return emails


def group_by_thread(emails):
    """Collapse messages sharing a threadId into one entry.

    Gmail lists ids newest-first, so the first message we see for a thread
    is its most recent one — we keep that as the thread's representative
    and only note how many messages the thread carried. A thread counts as
    VIP if any of its messages is. This keeps a busy back-and-forth from
    flooding the prompt (and the digest) as N near-identical lines.
    """
    grouped = {}
    order = []
    for e in emails:
        # Fall back to the per-message link when threadId is absent, so a
        # message without one is never silently merged with another.
        key = e.get("threadId") or e["link"]
        if key not in grouped:
            rep = dict(e)
            rep["thread_count"] = 1
            grouped[key] = rep
            order.append(key)
        else:
            rep = grouped[key]
            rep["thread_count"] += 1
            rep["vip"] = rep["vip"] or e["vip"]
    return [grouped[k] for k in order]


def vip_block(emails):
    """Code-generated '🔔 VIP' section, prepended to the digest.

    Built deterministically from the already-computed e['vip'] flag so VIP
    mail is GUARANTEED to surface — it does not depend on the model doing
    the right thing. Returns '' when there is no VIP mail.
    """
    vips = [e for e in emails if e["vip"]]
    if not vips:
        return ""
    lines = ["🔔 VIP"]
    for e in vips:
        lines.append(f"• {e['from']} — {e['subject']}")
        lines.append(f"  {e['link']}")
    return "\n".join(lines) + "\n\n"


_LINK_RE = re.compile(r"https://mail\.google\.com/\S+")


def validate_links(digest, valid_links):
    """Strip any Gmail deep link the model emitted that isn't one of the
    real per-message links.

    The prompt asks the model to copy links verbatim and never invent one,
    but nothing enforces it — a hallucinated link would send me to the
    wrong message (or nowhere). Here we drop any link that isn't in the
    real candidate set, keeping trailing punctuation intact.
    """
    valid = set(valid_links)

    def repl(m):
        url = m.group(0)
        trailing = ""
        while url and url[-1] in ".,;:)]}\"'":
            trailing = url[-1] + trailing
            url = url[:-1]
        if url in valid:
            return url + trailing
        return "[invalid link removed]" + trailing

    return _LINK_RE.sub(repl, digest)


STATE_FILE = BASE_DIR / "state" / "noise.json"
NOISE_DAYS = 14  # history window for unsubscribe suggestions
NOISE_THRESHOLD = 5  # noise appearances in the window that earn a suggestion
STATE_MARKER = "===STATE==="


def load_noise():
    """{sender: [dates it was filed as noise]}, pruned to the window."""
    try:
        noise = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = (datetime.now(IST) - timedelta(days=NOISE_DAYS)).strftime("%Y-%m-%d")
    out = {}
    for sender, dates in noise.items():
        kept = sorted(d for d in dates if isinstance(d, str) and d >= cutoff)
        if kept:
            out[sender] = kept
    return out


def save_noise(noise):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(noise, indent=1, sort_keys=True) + "\n")


def split_state(reply):
    """(digest text, noise-sender list) — the model appends a JSON tail
    after STATE_MARKER; a malformed tail costs the trend data, never the
    digest."""
    if STATE_MARKER not in reply:
        return reply.strip(), []
    text, _, tail = reply.partition(STATE_MARKER)
    start, end = tail.find("{"), tail.rfind("}")
    senders = []
    if start != -1 and end > start:
        try:
            senders = json.loads(tail[start : end + 1]).get("noise_senders", [])
        except (ValueError, AttributeError):
            senders = []
    return text.strip(), [s for s in senders if isinstance(s, str)]


def unsubscribe_block(noise):
    """Senders that have been pure noise most days lately — worth ending."""
    candidates = sorted(
        (sender, len(dates))
        for sender, dates in noise.items()
        if len(dates) >= NOISE_THRESHOLD
    )
    if not candidates:
        return ""
    lines = ["📉 Unsubscribe candidates (noise on N of the last 14 days):"]
    lines += [f"• {sender} — {n} days" for sender, n in candidates]
    return "\n".join(lines)


def summarize(emails):
    """One model call: the day's mail sorted by what it needs from me.

    Expects thread-grouped entries (see group_by_thread): one line per
    conversation, not per message.
    """
    lines = []
    for e in emails:
        vip = " [VIP]" if e["vip"] else ""
        n = e.get("thread_count", 1)
        thread = f" ({n} msgs in thread)" if n > 1 else ""
        lines.append(
            f"- From: {e['from']}{vip} | Subject: {e['subject']}{thread} | "
            f"Snippet: {e['snippet']} | Link: {e['link']}"
        )
    email_lines = "\n".join(lines)
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
        "never NOISE.\n\n"
        f"Then output the line {STATE_MARKER} and ONE JSON object: "
        '{"noise_senders": [the bare email addresses of the senders you '
        "counted as NOISE]}. No text after the JSON."
    )
    # Explicit, larger cap: a heavy mail day must not silently truncate the
    # digest mid-list the way the 2000-token default could.
    return ask_llm(prompt, max_tokens=4000)


def main():
    load_dotenv(BASE_DIR / ".env")
    start, end = digest_window()

    service = gmail_service()
    emails = fetch_emails(service, start, end)
    threads = group_by_thread(emails)

    header = (
        f"📬 Mail digest — {end:%a %d %b %Y}\n"
        f"(window {start:%H:%M %d %b} → {end:%H:%M %d %b} IST, "
        f"{len(emails)} emails)\n\n"
    )
    noise = load_noise()
    noise_today = []
    if not threads:
        body = "Quiet inbox: no email in 24h ☕"
    else:
        digest, noise_today = split_state(summarize(threads))
        digest = validate_links(digest, [e["link"] for e in emails])
        # Deterministic VIP block first, then the model's digest.
        body = vip_block(threads) + digest
    # Sundays: surface the senders that have been pure noise all week —
    # the actionable follow-up to two weeks of NOISE counts.
    if datetime.now(IST).weekday() == 6:
        block = unsubscribe_block(noise)
        if block:
            body += "\n\n" + block
    send_telegram(header + body)

    # Record today's noise senders — after the send, so a state failure
    # never costs the digest itself.
    today = datetime.now(IST).strftime("%Y-%m-%d")
    for sender in noise_today:
        days = noise.setdefault(sender.strip().lower(), [])
        if today not in days:
            days.append(today)
    try:
        save_noise(noise)
    except OSError:
        pass


if __name__ == "__main__":
    main()
