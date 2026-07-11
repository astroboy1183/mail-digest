# mail-digest

Gmail inbox guardian → Telegram, two editions: the full digest at
~6:07 AM IST and a 19:00 evening sweep that is SILENT unless can't-wait
mail arrived since morning. One agent, one task, one bot:
`@jayanth_morning_email_bot`.

Beyond summarizing, it follows through: action items are remembered and
re-surface with age tags until your sent mail proves you replied
(🔁 CARRIED); stated deadlines join a standing ledger that reappears
every morning until the date passes (📅 Ahead); security alerts get
their own 🚨 section; 📎 mail shows its attachment filenames; and
Sundays add a week-over-week scorecard plus VIP suggestions computed
from who you actually reply to.

```
📬 Mail — Fri 10 Jul
23 emails (9 unread) · 6 AM Thu → 6 AM Fri

🔔 VIP
• Boss <boss@co.in> — Re: Q3 budget (2 msgs) 📎
  link

⚡ NEEDS ACTION — 2
1. ⏰ Mon — LIC premium ₹12,400 due (LIC)
   link
2. reply — Rahul asking about weekend plans (unread)
   link

📥 FYI — 3 (capped at 6, "…and N more")
• HDFC card statement generated 📎
  link
…

⏳ STILL UNREAD — older than 2 days
• Tue — LIC renewal notice (lic@licindia.com)
  link

🗑 14 noise — 9 promos · 3 updates · 2 social

(Sundays add:)
📊 This week: 143 emails · 61% noise · busiest: amazon (12) …
📉 Unsubscribe candidates …
```

Every email shown carries its validated deep link. Unread and 📎 flags
come from Gmail labels + one has:attachment query; the noise count is
deterministic Python over Gmail's own category labels (the model never
counts); ⏳ surfaces inbox mail 2-14 days old still sitting unread — the
mail the 24h window can't see.

## How the code works

`mail_digest.py`, in pipeline order:

- **`digest_window()`** — returns the 24h window ending at the most
  recent 6:00 AM IST. It is *anchored*, not rolling: a run delayed to
  7:30 still covers exactly the same 6AM→6AM day, so the primary cron and
  the backup cron can never produce gaps or overlaps between days.
- **`gmail_service()`** — builds an authenticated Gmail client from
  `token.json` (OAuth refresh token). Expired tokens refresh silently.
  If there's no usable token: locally it opens a browser consent flow
  (`InstalledAppFlow.run_local_server`); in CI it raises immediately with
  instructions instead — GitHub Actions has no browser, and hanging
  forever is worse than a loud failure.
- **`fetch_emails(service, start, end)`** — one Gmail search
  (`after:<epoch> before:<epoch> -category:spam`), paginated 100 ids at a
  time, then a metadata-only fetch per message (From, Subject + Gmail's
  own snippet). Metadata format keeps it fast and avoids downloading
  bodies. Each email also gets a deep link
  (`mail.google.com/mail/u/0/#all/<id>`) straight to the message, its
  `threadId`, and a VIP flag when the sender matches `VIP_SENDERS`
  (substring, case-insensitive). The list comes from the optional
  `VIP_SENDERS` secret as comma-separated fragments — never from code,
  since this repo is public.
- **`group_by_thread(emails)`** — collapses messages sharing a
  `threadId` into one entry before summarizing. Gmail lists ids
  newest-first, so the first message seen for a thread is its most recent
  one and becomes the representative; the entry records how many messages
  the thread carried, and is VIP if any of its messages is. A long
  back-and-forth becomes one digest line instead of N — less prompt
  bloat, cleaner output.
- **`summarize(emails)`** — a single model call over the thread-grouped
  entries. The prompt pastes each as one line and demands a fixed output
  shape: one headline, then NEEDS ACTION (reply/decision/deadline, each
  with its deep link on its own line), FYI (grouped by sender), NOISE
  (one count line). VIP mail may never land in NOISE. Called with an
  explicit `max_tokens=4000` (up from the 2000 default) so a heavy mail
  day can't silently truncate the digest mid-list. An empty inbox skips
  the model entirely ("Quiet inbox ☕").
- **`vip_block(emails)`** — a code-generated `🔔 VIP` section (sender +
  subject + link per VIP thread) built deterministically from the `vip`
  flag and prepended to the digest. VIP mail is therefore *guaranteed*
  visible regardless of what the model does — the prompt instruction is
  now only a backstop, not the sole guard.
- **`validate_links(digest, real_links)`** — strips any Gmail deep link
  the model emitted that isn't one of the real per-message links,
  enforcing the "copy links verbatim, never invent one" rule the prompt
  can only ask for. A hallucinated link becomes `[invalid link removed]`.
- **`main()`** — window → fetch → group → summarize → validate links →
  prepend VIP block → send, with a header that states the exact window
  and (raw, ungrouped) email count, so a delayed run is honest about what
  it covered.
- **`agentlib.py`** (vendored) — `ask_llm()` one-shot model call;
  `send_telegram()` chunked sends.

## Design notes

- The workflow's "Restore Gmail OAuth files" step materializes
  `credentials.json` / `token.json` from repo secrets on every run — the
  files are never committed (see `.gitignore`).
- Two crons + dedupe guard: backup at 07:07 IST delivers only if the
  06:07 primary was dropped or failed.

- **Unsubscribe suggestions (Sundays)**: the model reports each day's
  noise senders via a hidden `===STATE===` tail into `state/noise.json`
  (committed back by the workflow); senders that were noise on 5+ of
  the last 14 days get listed as unsubscribe candidates.

- Tests run in CI on every push (`.github/workflows/tests.yml`).

## Ops

- Schedule: `.github/workflows/mail-digest.yml`
  (`37 0 * * *` UTC = 06:07 IST; backup 07:07)
- Run now: `gh workflow run mail-digest.yml -R astroboy1183/mail-digest`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`, `GMAIL_CREDENTIALS_JSON`, `GMAIL_TOKEN_JSON`,
  optional `VIP_SENDERS` (comma-separated sender fragments)
- Gmail token expired? `cd ~/agents/mail_digest && .venv/bin/python
  mail_digest.py` locally (opens browser), then update the
  `GMAIL_TOKEN_JSON` secret from the fresh `token.json`.
