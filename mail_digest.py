#!/usr/bin/env python3
"""Mail digest.

One Telegram message every morning (~6:07 IST via GitHub Actions) — an
inbox guardian, not just a traffic summary:

  🔔 VIP            — code-guaranteed block for senders on the VIP list
  ⚡ NEEDS ACTION   — numbered, deadline-first, deep link per item
  📥 FYI            — capped at 6, deep link per item
  ⏳ STILL UNREAD   — inbox mail 2-14 days old still sitting unread (the
                      24h window can't see it; this is where mail rots)
  🗑 noise          — deterministic count from Gmail's own category
                      labels (promos · updates · social · forums)
  📊 / 📉 Sundays   — week volume/noise stats + unsubscribe candidates

Every email shown anywhere carries its deep link into Gmail, validated
against the real per-message links so a hallucinated link can never
reach me. Unread and 📎-attachment flags ride along from Gmail labels
and one extra has:attachment query.

One agent, one task, one bot (@jayanth_morning_email_bot).

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

# Gmail's own inbox categories — the deterministic backbone of the noise
# count. The model never counts noise; Python does, from these labels.
CATEGORY_LABELS = {
    "CATEGORY_PROMOTIONS": "promos",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_FORUMS": "forums",
}

FYI_CAP = 6            # FYI items shown before "…and N more"
STALE_UNREAD_CAP = 5   # oldest still-unread items shown
STALE_UNREAD_QUERY = "in:inbox is:unread older_than:2d newer_than:14d"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def bare_email(from_header):
    """'Name <a@b.c>' → 'a@b.c' (lowered); falls back to the whole header."""
    m = EMAIL_RE.search(from_header or "")
    return m.group(0).lower() if m else (from_header or "").lower()


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


def _list_ids(service, query, cap=None):
    """All message ids matching a Gmail search (paginated, optional cap)."""
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
        if not page_token or (cap and len(ids) >= cap):
            break
    return ids[:cap] if cap else ids


def _fetch_meta(service, msg_id):
    """One message's metadata → our email dict (labels drive the flags)."""
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
    labels = set(msg.get("labelIds", []))
    return {
        "from": sender,
        "sender_email": bare_email(sender),
        "subject": headers.get("Subject", "(no subject)"),
        "snippet": msg.get("snippet", ""),
        # deep link straight to the message in the Gmail web UI
        "link": f"https://mail.google.com/mail/u/0/#all/{msg_id}",
        "vip": any(v.lower() in sender.lower() for v in VIP_SENDERS),
        "unread": "UNREAD" in labels,
        "categories": sorted(
            CATEGORY_LABELS[l] for l in labels if l in CATEGORY_LABELS
        ),
        "attach": False,  # filled in by the has:attachment pass
        # epoch ms — used to date the still-unread block
        "ts": int(msg.get("internalDate", 0)),
        # thread this message belongs to — messages in the same
        # conversation are collapsed to one digest entry downstream
        "threadId": msg.get("threadId"),
    }


def fetch_emails(service, start, end):
    """Every message in [start, end) — read or unread — minus spam.

    A second, cheap search marks which of them carry attachments: 📎 is
    a one-query flag, not a per-message metadata dig."""
    base = f"after:{int(start.timestamp())} before:{int(end.timestamp())} -category:spam"
    emails = [_fetch_meta(service, mid) for mid in _list_ids(service, base)]
    try:
        with_attach = set(_list_ids(service, base + " has:attachment"))
        for e in emails:
            e["attach"] = e["link"].rsplit("/", 1)[-1] in with_attach
    except Exception:
        pass  # attachment flags are an enrichment, never worth a dead run
    return emails


def fetch_still_unread(service):
    """(items, extra_count): oldest inbox mail 2-14 days old, still unread.

    The daily window only sees 24h — this is the mail quietly rotting
    beyond it. Oldest first, capped, with the overflow counted."""
    ids = _list_ids(service, STALE_UNREAD_QUERY, cap=25)
    items = sorted(
        (_fetch_meta(service, mid) for mid in ids), key=lambda e: e["ts"]
    )
    return items[:STALE_UNREAD_CAP], max(0, len(items) - STALE_UNREAD_CAP)


def group_by_thread(emails):
    """Collapse messages sharing a threadId into one entry.

    Gmail lists ids newest-first, so the first message we see for a thread
    is its most recent one — we keep that as the thread's representative
    and only note how many messages the thread carried. A thread counts as
    VIP/unread/📎 if any of its messages is. This keeps a busy
    back-and-forth from flooding the prompt as N near-identical lines.
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
            rep["unread"] = rep["unread"] or e["unread"]
            rep["attach"] = rep["attach"] or e["attach"]
    return [grouped[k] for k in order]


def vip_block(emails):
    """Code-generated '🔔 VIP' section.

    Built deterministically from the already-computed e['vip'] flag so VIP
    mail is GUARANTEED to surface — it does not depend on the model doing
    the right thing. Returns '' when there is no VIP mail.
    """
    vips = [e for e in emails if e["vip"]]
    if not vips:
        return ""
    lines = ["🔔 VIP"]
    for e in vips:
        n = e.get("thread_count", 1)
        marks = "".join(
            [" 📎" if e.get("attach") else "", f" ({n} msgs)" if n > 1 else ""]
        )
        lines.append(f"• {e['from']} — {e['subject']}{marks}")
        lines.append(f"  {e['link']}")
    return "\n".join(lines)


def noise_line(emails):
    """'🗑 14 noise — 9 promos · 3 updates · 2 social' from Gmail's own
    category labels. Deterministic Python; the model never counts."""
    counts = {}
    for e in emails:
        for cat in e.get("categories", []):
            counts[cat] = counts.get(cat, 0) + 1
    total = sum(counts.values())
    if not total:
        return ""
    parts = " · ".join(
        f"{n} {cat}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1])
    )
    return f"🗑 {total} noise — {parts}"


def still_unread_block(items, extra):
    """'⏳ STILL UNREAD' section from fetch_still_unread's findings."""
    if not items:
        return ""
    lines = ["⏳ STILL UNREAD — older than 2 days"]
    for e in items:
        day = datetime.fromtimestamp(e["ts"] / 1000, IST).strftime("%a") if e["ts"] else "?"
        lines.append(f"• {day} — {e['subject']} ({e['sender_email']})")
        lines.append(f"  {e['link']}")
    if extra:
        lines.append(f"…and {extra} more")
    return "\n".join(lines)


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
STATS_FILE = BASE_DIR / "state" / "stats.json"
NOISE_DAYS = 14  # history window for unsubscribe suggestions
NOISE_THRESHOLD = 5  # noise appearances in the window that earn a suggestion
STATS_DAYS = 8  # daily volume records kept (a week + margin)
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


def load_stats():
    """{date: {total, noise, senders{email: n}}}, pruned to STATS_DAYS."""
    try:
        stats = json.loads(STATS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    cutoff = (datetime.now(IST) - timedelta(days=STATS_DAYS)).strftime("%Y-%m-%d")
    return {d: v for d, v in stats.items() if isinstance(d, str) and d >= cutoff}


def save_stats(stats):
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATS_FILE.write_text(json.dumps(stats, indent=1, sort_keys=True) + "\n")


def record_stats(stats, day, emails):
    """Fold one day's volume into the stats state (top 5 senders kept)."""
    senders = {}
    noise_count = 0
    for e in emails:
        senders[e["sender_email"]] = senders.get(e["sender_email"], 0) + 1
        if e.get("categories"):
            noise_count += 1
    top = dict(sorted(senders.items(), key=lambda kv: -kv[1])[:5])
    stats[day] = {"total": len(emails), "noise": noise_count, "senders": top}
    return stats


def weekly_block(stats):
    """'📊 This week: …' — Sunday-only, deterministic from the stats state."""
    if not stats:
        return ""
    total = sum(v.get("total", 0) for v in stats.values())
    noise = sum(v.get("noise", 0) for v in stats.values())
    senders = {}
    for v in stats.values():
        for s, n in v.get("senders", {}).items():
            senders[s] = senders.get(s, 0) + n
    top = sorted(senders.items(), key=lambda kv: -kv[1])[:3]
    busiest = ", ".join(f"{s} ({n})" for s, n in top)
    pct = round(noise / total * 100) if total else 0
    return f"📊 This week: {total} emails · {pct}% noise · busiest: {busiest}"


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
    lines = ["📉 Unsubscribe candidates (noise-heavy, last 14 days):"]
    lines += [f"• {sender} — {n} days" for sender, n in candidates]
    return "\n".join(lines)


def summarize(emails):
    """One model call: the day's mail → headline + ⚡ NEEDS ACTION + 📥 FYI.

    Expects thread-grouped entries (see group_by_thread): one line per
    conversation, not per message. Noise is NOT the model's job — Gmail's
    category labels are counted in code — but the model still names noise
    senders in the STATE tail for the unsubscribe memory.
    """
    lines = []
    for e in emails:
        flags = "".join(
            [
                " [VIP]" if e["vip"] else "",
                " [UNREAD]" if e.get("unread") else "",
                " [ATTACH]" if e.get("attach") else "",
                f" [{'/'.join(e['categories'])}]" if e.get("categories") else "",
            ]
        )
        n = e.get("thread_count", 1)
        thread = f" ({n} msgs in thread)" if n > 1 else ""
        lines.append(
            f"- From: {e['from']}{flags} | Subject: {e['subject']}{thread} | "
            f"Snippet: {e['snippet']} | Link: {e['link']}"
        )
    email_lines = "\n".join(lines)
    prompt = (
        "You are composing my morning mail digest. Be terse. "
        "Plain text only — no markdown headers or bold.\n\n"
        "=== INPUT: every email thread from the last 24h "
        "(sender, flags, subject, snippet, link) ===\n"
        f"{email_lines}\n\n"
        "Produce EXACTLY this output structure:\n\n"
        "<one-line headline for the day's mail>\n\n"
        "⚡ NEEDS ACTION — <count>\n"
        "1. ⏰ <deadline: 'today' / 'Mon' / '15 Jul' — or 'reply' when it "
        "just needs an answer> — <what, concretely> (<short sender>"
        "<, unread if [UNREAD]>)<append 📎 if [ATTACH]>\n"
        "<that email's Link on its own line>\n"
        "…numbered, most urgent first. If none: '⚡ NEEDS ACTION — none 🎉' "
        "with no items.\n\n"
        "📥 FYI — <count>\n"
        "• <one-line summary> (<short sender><, N msgs if a thread>)\n"
        "<that email's Link on its own line>\n"
        f"…at most {FYI_CAP} items, most notable first; if more were "
        "worth listing, end the section with '…and N more'.\n\n"
        "Rules:\n"
        "- Emails tagged with a Gmail category ([promos]/[updates]/"
        "[social]/[forums]) are noise: leave them out entirely (the code "
        "reports noise separately) UNLESS one is genuinely important — a "
        "bill due, a security alert, a delivery today.\n"
        "- [VIP] emails must appear in ⚡ or 📥, never omitted.\n"
        "- Copy links verbatim — never invent one.\n"
        "- No other sections, no noise counts.\n\n"
        f"Then output the line {STATE_MARKER} and ONE JSON object: "
        '{"noise_senders": [the bare email addresses of the senders you '
        "treated as noise]}. No text after the JSON."
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
    try:
        stale, stale_extra = fetch_still_unread(service)
    except Exception:  # inbox-health extra must never kill the digest
        stale, stale_extra = [], 0

    unread_count = sum(1 for e in emails if e.get("unread"))
    header = (
        f"📬 Mail — {end:%a %d %b}\n"
        f"{len(emails)} emails ({unread_count} unread) · "
        f"6 AM {start:%a} → 6 AM {end:%a}"
    )

    noise = load_noise()
    stats = load_stats()
    noise_today = []
    parts = [header]
    if not threads:
        parts.append("Quiet inbox: no email in 24h ☕")
    else:
        digest, noise_today = split_state(summarize(threads))
        digest = validate_links(digest, [e["link"] for e in emails])
        vip = vip_block(threads)
        if vip:
            parts.append(vip)
        parts.append(digest)
    stale_part = still_unread_block(stale, stale_extra)
    if stale_part:
        parts.append(stale_part)
    trash = noise_line(emails)
    if trash:
        parts.append(trash)
    # Sundays: zoom out — week volume stats + the unsubscribe shortlist.
    if datetime.now(IST).weekday() == 6:
        week = weekly_block(stats)
        if week:
            parts.append(week)
        unsub = unsubscribe_block(noise)
        if unsub:
            parts.append(unsub)
    send_telegram("\n\n".join(parts))

    # Record state — after the send, so a state failure never costs the
    # digest itself.
    today = datetime.now(IST).strftime("%Y-%m-%d")
    for sender in noise_today:
        days = noise.setdefault(sender.strip().lower(), [])
        if today not in days:
            days.append(today)
    try:
        save_noise(noise)
        save_stats(record_stats(stats, f"{end:%Y-%m-%d}", emails))
    except OSError:
        pass


if __name__ == "__main__":
    main()
