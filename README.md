# Final Fantasy Football Bot

A small Discord bot built specifically for one Yahoo Fantasy Football league.

## What it does

- Uses **your existing Discord bot application**
- Authenticates directly with Yahoo OAuth 2.0
- Finds your 2026 Yahoo Fantasy Football league(s)
- Stores Yahoo refresh tokens encrypted in Postgres
- `/draft_countdown` reads the draft time directly from Yahoo and posts a Discord-native live countdown
- `/league_status` shows basic Yahoo league metadata
- `/standings` shows the current Yahoo standings
- Includes a tiny health web server on port `10000` for cloud hosting

## Required environment variables

- `DISCORD_TOKEN` — Discord bot token
- `YAHOO_KEY` — Yahoo Client ID / Consumer Key
- `YAHOO_SECRET` — Yahoo Client Secret
- `DATABASE_URL` — Postgres connection URI
- `BOT_ENCRYPTION_KEY` — Fernet key used to encrypt Yahoo tokens
- `PORT` — optional, defaults to `10000`
- `YAHOO_REDIRECT_URI` — optional; defaults to `https://oob` to match the Yahoo app you already created

You can reuse the fresh Fernet-style key you generated for Harambot, but only if it has never been exposed publicly.

## Discord setup

Enable:
- Server Members Intent
- Message Content Intent

The bot primarily uses slash commands, but leaving those enabled avoids gateway compatibility issues.

## First-time use

1. Run `/yahoo_login`
2. Open the Yahoo authorization link and approve access.
3. Yahoo shows you a short authorization code.
4. Run `/yahoo_code code:<the code>`
5. The bot discovers your 2026 NFL leagues.
6. If you only have one, it selects it automatically.
7. If you have more than one, run `/select_league`.
8. Run `/draft_countdown`

Yahoo's draft time is returned as a Unix timestamp, so Discord automatically renders it in each member's own timezone.

## Northflank

Use a Deployment Service from this repo/image. No Harambot cron job is required for the MVP.

Keep the existing Postgres addon and set the environment variables listed above.
