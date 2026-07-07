# mail-digest

Gmail 24h digest → Telegram, ~6:07 AM IST via GitHub Actions.

All mail from the previous 6AM→6AM IST window (anchored, so a delayed run
covers the same day), sorted into NEEDS ACTION / FYI / NOISE.

One agent, one task, one bot: `@jayanth_morning_email_bot`.
Part of the personal-agents fleet (`[gather] → [summarize] → [Telegram]`).

- Schedule: `.github/workflows/mail-digest.yml` (`37 0 * * *` UTC = 06:07 IST; backup 07:07 with dedupe guard)
- Run now: `gh workflow run mail-digest.yml -R astroboy1183/mail-digest`
- Secrets (Actions): `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
  `GMAIL_CREDENTIALS_JSON`, `GMAIL_TOKEN_JSON`
- Gmail token expired? Re-run the OAuth flow locally, then update the
  `GMAIL_TOKEN_JSON` secret from the fresh `token.json`.
