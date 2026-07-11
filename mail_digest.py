#!/usr/bin/env python3
"""Mail digest.

An inbox guardian in two editions:

  morning (~6:07 IST) — the full digest of the 6AM→6AM window:
    📅 AHEAD         — standing deadline ledger (a bill due Friday keeps
                       appearing until Friday, whenever it arrived)
    🔔 VIP           — code-guaranteed block for the VIP list
    ⚡ NEEDS ACTION  — numbered, deadline-first, deep link per item
    🔁 CARRIED       — yesterday's action items you HAVEN'T replied to
                       (verified against your sent mail), with age tags
    🚨 SECURITY      — sign-ins, password resets, new devices (omitted
                       when none)
    📥 FYI           — capped at 6, deep link per item
    ⏳ STILL UNREAD  — inbox mail 2-14 days old still sitting unread
    🗑 noise         — deterministic Gmail-category counts
    Sundays add: 📊 scorecard with week-over-week trends, 🔔 VIP
    suggestions (senders you demonstrably reply to), 📉 unsubscribe
    candidates.

  evening sweep (~19:00 IST) — SILENT unless action-needed or VIP mail
    arrived since 6 AM, so urgent noon mail doesn't wait 21 hours.

Every email shown anywhere carries its validated deep link. 📎 mail
carries its attachment filenames. Set MAIL_FORCE=1 to send regardless.

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

CATEGORY_LABELS = {
    "CATEGORY_PROMOTIONS": "promos",
    "CATEGORY_UPDATES": "updates",
    "CATEGORY_SOCIAL": "social",
    "CATEGORY_FORUMS": "forums",
}

FYI_CAP = 6            # FYI items shown before "…and N more"
STALE_UNREAD_CAP = 5   # oldest still-unread items shown
STALE_UNREAD_QUERY = "in:inbox is:unread older_than:2d newer_than:14d"
ATTACH_NAME_CAP = 5    # 📎 messages whose filenames get fetched (full format)
CARRY_DAYS = 14        # give up re-surfacing an action after this long
CARRY_CAP = 7          # carried items shown
AHEAD_DAYS = 14        # deadline ledger horizon
AHEAD_CAP = 5          # deadlines shown on the AHEAD line
VIP_SUGGEST_CAP = 3    # Sunday VIP suggestions
EVENING_HOUR = 15      # runs at/after this IST hour are the evening sweep

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


def sweep_window():
    """(start, now): today 6:00 AM IST → right now, for the evening sweep."""
    now = datetime.now(IST)
    return now.replace(hour=6, minute=0, second=0, microsecond=0), now


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
        "id": msg_id,
        "from": sender,
        "sender_email": bare_email(sender),
        "subject": headers.get("Subject", "(no subject)"),
        "snippet": msg.get("snippet", ""),
        "link": f"https://mail.google.com/mail/u/0/#all/{msg_id}",
        "vip": any(v.lower() in sender.lower() for v in VIP_SENDERS),
        "unread": "UNREAD" in labels,
        "categories": sorted(
            CATEGORY_LABELS[l] for l in labels if l in CATEGORY_LABELS
        ),
        "attach": False,   # filled by the has:attachment pass
        "files": [],       # filled for a capped number of 📎 messages
        "ts": int(msg.get("internalDate", 0)),
        "threadId": msg.get("threadId"),
    }


def attachment_names(service, msg_id):
    """Filenames carried by one message (full-format fetch, best-effort)."""
    try:
        msg = (
            service.users().messages().get(userId="me", id=msg_id, format="full")
        ).execute()
        names = []
        stack = [msg.get("payload", {})]
        while stack:
            part = stack.pop()
            if part.get("filename"):
                names.append(part["filename"])
            stack.extend(part.get("parts", []) or [])
        return names[:4]
    except Exception:
        return []


def fetch_emails(service, start, end):
    """Every message in [start, end) — read or unread — minus spam.

    A second, cheap search marks attachments; the filenames themselves are
    fetched for a capped handful (often the filename IS the information)."""
    base = f"after:{int(start.timestamp())} before:{int(end.timestamp())} -category:spam"
    emails = [_fetch_meta(service, mid) for mid in _list_ids(service, base)]
    try:
        with_attach = set(_list_ids(service, base + " has:attachment"))
        named = 0
        for e in emails:
            e["attach"] = e["id"] in with_attach
            if e["attach"] and not e["categories"] and named < ATTACH_NAME_CAP:
                e["files"] = attachment_names(service, e["id"])
                named += 1
    except Exception:
        pass  # attachment flags are an enrichment, never worth a dead run
    return emails


def fetch_still_unread(service):
    """(items, extra_count): oldest inbox mail 2-14 days old, still unread."""
    ids = _list_ids(service, STALE_UNREAD_QUERY, cap=25)
    items = sorted(
        (_fetch_meta(service, mid) for mid in ids), key=lambda e: e["ts"]
    )
    return items[:STALE_UNREAD_CAP], max(0, len(items) - STALE_UNREAD_CAP)


def thread_replied(service, thread_id, since_ms):
    """Did I send anything in this thread after `since_ms`? Best-effort."""
    try:
        t = (
            service.users()
            .threads()
            .get(userId="me", id=thread_id, format="metadata",
                 metadataHeaders=["From"])
            .execute()
        )
        for msg in t.get("messages", []):
            if "SENT" in msg.get("labelIds", []) and int(
                msg.get("internalDate", 0)
            ) > since_ms:
                return True
    except Exception:
        pass
    return False


def group_by_thread(emails):
    """Collapse messages sharing a threadId into one entry.

    Gmail lists ids newest-first, so the first message we see for a thread
    is its most recent one — we keep that as the thread's representative
    and only note how many messages the thread carried. A thread counts as
    VIP/unread/📎 if any of its messages is."""
    grouped = {}
    order = []
    for e in emails:
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
            rep["files"] = rep["files"] or e["files"]
    return [grouped[k] for k in order]


def vip_block(emails):
    """Code-generated '🔔 VIP' section — guaranteed, model-independent."""
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
        if e.get("files"):
            lines.append(f"  📎 {', '.join(e['files'])}")
        lines.append(f"  {e['link']}")
    return "\n".join(lines)


def noise_line(emails):
    """Deterministic noise counts from Gmail's own category labels."""
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
    """Strip any Gmail deep link the model emitted that isn't real."""
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


# --- state ---------------------------------------------------------------

STATE_DIR = BASE_DIR / "state"
NOISE_FILE = STATE_DIR / "noise.json"
STATS_FILE = STATE_DIR / "stats.json"
ACTIONS_FILE = STATE_DIR / "actions.json"
DEADLINES_FILE = STATE_DIR / "deadlines.json"
NOISE_DAYS = 14
NOISE_THRESHOLD = 5
STATS_DAYS = 15  # two weeks + margin → week-over-week trends
STATE_MARKER = "===STATE==="


def _load_json(path, default):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def _save_json(path, data):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")


def load_noise():
    noise = _load_json(NOISE_FILE, {})
    cutoff = (datetime.now(IST) - timedelta(days=NOISE_DAYS)).strftime("%Y-%m-%d")
    out = {}
    for sender, dates in noise.items():
        kept = sorted(d for d in dates if isinstance(d, str) and d >= cutoff)
        if kept:
            out[sender] = kept
    return out


def load_stats():
    stats = _load_json(STATS_FILE, {})
    cutoff = (datetime.now(IST) - timedelta(days=STATS_DAYS)).strftime("%Y-%m-%d")
    return {d: v for d, v in stats.items() if isinstance(d, str) and d >= cutoff}


def load_actions():
    """Open action items, pruned to CARRY_DAYS."""
    cutoff_ms = int(
        (datetime.now(IST) - timedelta(days=CARRY_DAYS)).timestamp() * 1000
    )
    return [
        a
        for a in _load_json(ACTIONS_FILE, [])
        if isinstance(a, dict) and a.get("first_ms", 0) >= cutoff_ms
    ]


def load_deadlines():
    """Deadline ledger, past dates pruned."""
    today = datetime.now(IST).strftime("%Y-%m-%d")
    return [
        d
        for d in _load_json(DEADLINES_FILE, [])
        if isinstance(d, dict) and d.get("date", "") >= today
    ]


def record_stats(stats, day, emails, stale_count):
    senders = {}
    noise_count = 0
    for e in emails:
        senders[e["sender_email"]] = senders.get(e["sender_email"], 0) + 1
        if e.get("categories"):
            noise_count += 1
    top = dict(sorted(senders.items(), key=lambda kv: -kv[1])[:5])
    stats[day] = {
        "total": len(emails),
        "noise": noise_count,
        "stale": stale_count,
        "senders": top,
    }
    return stats


def split_state(reply):
    """(digest text, noise senders, action items) from the model's tail."""
    if STATE_MARKER not in reply:
        return reply.strip(), [], []
    text, _, tail = reply.partition(STATE_MARKER)
    start, end = tail.find("{"), tail.rfind("}")
    senders, actions = [], []
    if start != -1 and end > start:
        try:
            parsed = json.loads(tail[start : end + 1])
            senders = [
                s for s in parsed.get("noise_senders", []) if isinstance(s, str)
            ]
            actions = [
                a
                for a in parsed.get("actions", [])
                if isinstance(a, dict) and a.get("what") and a.get("link")
            ]
        except (ValueError, AttributeError):
            pass
    return text.strip(), senders, actions


# --- ledgers → message blocks ---------------------------------------------


def ahead_block(deadlines):
    """'📅 Ahead: Mon 13 — LIC ₹12,400 · …' from the deadline ledger."""
    horizon = (datetime.now(IST) + timedelta(days=AHEAD_DAYS)).strftime("%Y-%m-%d")
    upcoming = sorted(
        (d for d in deadlines if d["date"] <= horizon), key=lambda d: d["date"]
    )[:AHEAD_CAP]
    if not upcoming:
        return ""
    parts = []
    for d in upcoming:
        day = datetime.strptime(d["date"], "%Y-%m-%d").strftime("%a %d")
        parts.append(f"{day} — {d['what'][:60]}")
    return "📅 Ahead: " + " · ".join(parts)


def carried_block(open_actions, today_links):
    """'🔁 CARRIED' section: prior action items still awaiting my reply."""
    now_ms = int(datetime.now(IST).timestamp() * 1000)
    carried = [
        a for a in open_actions if a.get("link") not in today_links
    ][:CARRY_CAP]
    if not carried:
        return ""
    lines = ["🔁 CARRIED — awaiting your reply"]
    for a in carried:
        age_d = max(1, round((now_ms - a.get("first_ms", now_ms)) / 86_400_000))
        lines.append(f"• {age_d}d — {a['what'][:70]}")
        lines.append(f"  {a['link']}")
    return "\n".join(lines)


def merge_actions(open_actions, new_actions, emails_by_link):
    """Enrich model-reported actions with thread/date facts and merge.

    Only actions whose link matches a real email survive (the link IS the
    identity); duplicates keep their original first-seen date."""
    known = {a["link"] for a in open_actions}
    now_ms = int(datetime.now(IST).timestamp() * 1000)
    for a in new_actions:
        e = emails_by_link.get(a.get("link"))
        if e is None or a["link"] in known:
            continue
        open_actions.append(
            {
                "link": a["link"],
                "what": str(a.get("what"))[:100],
                "sender": e["sender_email"],
                "thread": e.get("threadId"),
                "first_ms": now_ms,
            }
        )
        known.add(a["link"])
    return open_actions


def merge_deadlines(deadlines, new_actions):
    """Fold dated actions into the ledger (deduped by link+date)."""
    known = {(d.get("link"), d.get("date")) for d in deadlines}
    for a in new_actions:
        date = a.get("deadline")
        if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date)):
            continue
        key = (a.get("link"), date)
        if key in known:
            continue
        deadlines.append(
            {"date": date, "what": str(a.get("what"))[:60], "link": a["link"]}
        )
        known.add(key)
    return deadlines


def weekly_block(stats):
    """Sunday scorecard with week-over-week trends."""
    if not stats:
        return ""
    days = sorted(stats)
    this_week = [stats[d] for d in days[-7:]]
    last_week = [stats[d] for d in days[:-7]][-7:]

    def totals(week):
        t = sum(v.get("total", 0) for v in week)
        n = sum(v.get("noise", 0) for v in week)
        return t, n

    t_now, n_now = totals(this_week)
    pct = round(n_now / t_now * 100) if t_now else 0
    senders = {}
    for v in this_week:
        for s, n in v.get("senders", {}).items():
            senders[s] = senders.get(s, 0) + n
    busiest = ", ".join(
        f"{s} ({n})" for s, n in sorted(senders.items(), key=lambda kv: -kv[1])[:3]
    )
    line = f"📊 Week: {t_now} emails · {pct}% noise · busiest: {busiest}"
    if last_week:
        t_prev, _ = totals(last_week)
        if t_prev:
            delta = round((t_now - t_prev) / t_prev * 100)
            line += f" · volume {'↑' if delta > 0 else '↓' if delta < 0 else '→'}{abs(delta)}%"
    stale_now = this_week[-1].get("stale") if this_week else None
    if stale_now is not None:
        line += f" · still-unread pile: {stale_now}"
    return line


def unsubscribe_block(noise):
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


def vip_suggestions(service, stats, noise):
    """Sunday: senders you demonstrably reply to → VIP candidates.

    Candidates: frequent, non-noise, not already VIP. Evidence: at least
    two replies from me in the last 15 days (one sent-mail search per
    candidate, capped)."""
    counts = {}
    for v in stats.values():
        for s, n in v.get("senders", {}).items():
            counts[s] = counts.get(s, 0) + n
    candidates = [
        s
        for s, n in sorted(counts.items(), key=lambda kv: -kv[1])
        if n >= 3
        and s not in noise
        and not any(v.lower() in s for v in VIP_SENDERS)
    ][:5]
    lines = []
    for s in candidates:
        try:
            replies = len(
                _list_ids(service, f"in:sent to:{s} newer_than:15d", cap=5)
            )
        except Exception:
            continue
        if replies >= 2:
            lines.append(f"• {s} — you replied {replies}× recently")
        if len(lines) >= VIP_SUGGEST_CAP:
            break
    if not lines:
        return ""
    return "🔔 Consider adding to VIP_SENDERS:\n" + "\n".join(lines)


# --- model calls -----------------------------------------------------------


def _email_lines(emails):
    lines = []
    for e in emails:
        flags = "".join(
            [
                " [VIP]" if e["vip"] else "",
                " [UNREAD]" if e.get("unread") else "",
                (
                    f" [ATTACH: {', '.join(e['files'])}]"
                    if e.get("files")
                    else (" [ATTACH]" if e.get("attach") else "")
                ),
                f" [{'/'.join(e['categories'])}]" if e.get("categories") else "",
            ]
        )
        n = e.get("thread_count", 1)
        thread = f" ({n} msgs in thread)" if n > 1 else ""
        lines.append(
            f"- From: {e['from']}{flags} | Subject: {e['subject']}{thread} | "
            f"Snippet: {e['snippet']} | Link: {e['link']}"
        )
    return "\n".join(lines)


STATE_TAIL_SPEC = (
    f"Then output the line {STATE_MARKER} and ONE JSON object:\n"
    '{"noise_senders": [bare addresses of senders you treated as noise],\n'
    ' "actions": [for EVERY item you put under NEEDS ACTION: '
    '{"what": "terse imperative", "link": "that email\'s link", '
    '"deadline": "YYYY-MM-DD or null — only when the email states a real '
    'date"}]}\n'
    "No text after the JSON."
)


def summarize(emails):
    """Morning model call: headline + ⚡ + 🚨 + 📥, with the state tail."""
    today = datetime.now(IST).strftime("%Y-%m-%d (%A)")
    prompt = (
        "You are composing my morning mail digest. Be terse. Plain text "
        f"only — no markdown headers or bold. Today is {today}.\n\n"
        "=== INPUT: every email thread from the last 24h ===\n"
        f"{_email_lines(emails)}\n\n"
        "Produce EXACTLY this output structure:\n\n"
        "<one-line headline for the day's mail>\n\n"
        "⚡ NEEDS ACTION — <count>\n"
        "1. ⏰ <deadline like 'today'/'Mon'/'15 Jul' — or 'reply' when it "
        "just needs an answer> — <what, concretely> (<short sender>"
        "<, unread if [UNREAD]>)<append 📎 + filenames if [ATTACH…]>\n"
        "<that email's Link on its own line>\n"
        "…numbered, most urgent first. If none: '⚡ NEEDS ACTION — none 🎉'.\n\n"
        "🚨 SECURITY — sign-ins, password resets, new-device or recovery "
        "alerts, one line each + link. OMIT this section entirely when "
        "there are none. Security mail is NEVER noise.\n\n"
        "📥 FYI — <count>\n"
        "• <one-line summary> (<short sender><, N msgs if a thread>)\n"
        "<that email's Link on its own line>\n"
        f"…at most {FYI_CAP} items; if more were worth listing, end with "
        "'…and N more'.\n\n"
        "Rules:\n"
        "- Gmail-categorized mail ([promos]/[updates]/[social]/[forums]) "
        "is noise: leave it out (the code reports noise separately) UNLESS "
        "genuinely important — a bill due, a security alert, a delivery "
        "today.\n"
        "- [VIP] emails must appear in ⚡ or 📥, never omitted.\n"
        "- Copy links verbatim — never invent one.\n"
        "- No other sections, no noise counts.\n\n" + STATE_TAIL_SPEC
    )
    return ask_llm(prompt, max_tokens=4000)


def sweep(emails):
    """Evening model call: only what can't wait for tomorrow morning."""
    prompt = (
        "You are my evening mail sweep: mail that arrived since 6 AM. "
        "Report ONLY what should not wait for tomorrow's 6 AM digest — "
        "action needed today/tomorrow morning, VIP mail, security alerts. "
        "Plain text, terse.\n\n"
        f"{_email_lines(emails)}\n\n"
        "If anything qualifies, produce:\n"
        "⚡ <count> can't-wait item(s)\n"
        "1. <what + why it can't wait> (<short sender>)\n"
        "<that email's Link on its own line>\n\n"
        "If NOTHING genuinely qualifies, output exactly: NONE\n\n"
        + STATE_TAIL_SPEC.replace("NEEDS ACTION", "the can't-wait list")
    )
    return ask_llm(prompt, max_tokens=1500)


# --- entry point -------------------------------------------------------------


def main():
    load_dotenv(BASE_DIR / ".env")
    now = datetime.now(IST)
    forced = bool(os.environ.get("MAIL_FORCE"))
    evening = now.hour >= EVENING_HOUR

    service = gmail_service()
    noise = load_noise()
    actions = load_actions()
    deadlines = load_deadlines()

    if evening:
        start, end = sweep_window()
        emails = fetch_emails(service, start, end)
        threads = group_by_thread(emails)
        if not threads and not forced:
            print("evening sweep: no mail since 6 AM — staying silent")
            return
        reply = sweep(threads) if threads else "NONE"
        text, _, new_actions = split_state(reply)
        if text.strip() == "NONE" and not forced:
            print("evening sweep: nothing that can't wait — staying silent")
            return
        body = validate_links(text, [e["link"] for e in emails])
        if text.strip() == "NONE":
            body = "Nothing that can't wait (forced send)."
        send_telegram(f"📬 Mail — evening sweep ({now:%a %d %b})\n\n{body}")
        # can't-wait items join the ledgers so tomorrow can carry them
        by_link = {e["link"]: e for e in emails}
        _save_json(ACTIONS_FILE, merge_actions(actions, new_actions, by_link))
        _save_json(DEADLINES_FILE, merge_deadlines(deadlines, new_actions))
        return

    # ---- morning: the full digest ----
    start, end = digest_window()
    emails = fetch_emails(service, start, end)
    threads = group_by_thread(emails)
    try:
        stale, stale_extra = fetch_still_unread(service)
    except Exception:
        stale, stale_extra = [], 0
    stats = load_stats()

    # Follow-through: drop carried actions I've already replied to.
    open_actions = [
        a
        for a in actions
        if not (
            a.get("thread") and thread_replied(service, a["thread"], a["first_ms"])
        )
    ]

    unread_count = sum(1 for e in emails if e.get("unread"))
    header = (
        f"📬 Mail — {end:%a %d %b}\n"
        f"{len(emails)} emails ({unread_count} unread) · "
        f"6 AM {start:%a} → 6 AM {end:%a}"
    )

    noise_today, new_actions = [], []
    parts = [header]
    ahead = ahead_block(deadlines)
    if ahead:
        parts.append(ahead)
    if not threads:
        parts.append("Quiet inbox: no email in 24h ☕")
    else:
        digest, noise_today, new_actions = split_state(summarize(threads))
        digest = validate_links(digest, [e["link"] for e in emails])
        vip = vip_block(threads)
        if vip:
            parts.append(vip)
        parts.append(digest)
    today_links = {a.get("link") for a in new_actions}
    carried = carried_block(open_actions, today_links)
    if carried:
        parts.append(carried)
    stale_part = still_unread_block(stale, stale_extra)
    if stale_part:
        parts.append(stale_part)
    trash = noise_line(emails)
    if trash:
        parts.append(trash)
    if now.weekday() == 6:  # Sundays: zoom out
        week = weekly_block(stats)
        if week:
            parts.append(week)
        try:
            suggest = vip_suggestions(service, stats, noise)
        except Exception:
            suggest = ""
        if suggest:
            parts.append(suggest)
        unsub = unsubscribe_block(noise)
        if unsub:
            parts.append(unsub)
    send_telegram("\n\n".join(parts))

    # Record state — after the send, so a state failure never costs the
    # digest itself.
    today = now.strftime("%Y-%m-%d")
    for sender in noise_today:
        days = noise.setdefault(sender.strip().lower(), [])
        if today not in days:
            days.append(today)
    by_link = {e["link"]: e for e in emails}
    try:
        _save_json(NOISE_FILE, noise)
        _save_json(STATS_FILE, record_stats(stats, f"{end:%Y-%m-%d}", emails,
                                            stale_extra + len(stale)))
        _save_json(ACTIONS_FILE, merge_actions(open_actions, new_actions, by_link))
        _save_json(DEADLINES_FILE, merge_deadlines(deadlines, new_actions))
    except OSError:
        pass


if __name__ == "__main__":
    main()
