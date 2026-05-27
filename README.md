# Sesly Bot — WhatsApp AI Webhook (Meta Cloud API)

Multi-tenant WhatsApp bot for Sesly. Receives Meta WhatsApp Cloud API
webhooks, routes by `/handle` to the correct bot, transcribes voice notes
with Whisper, and replies with Claude.

## Deploy to Vercel

1. **Vercel → Add New → Project** → import this repo.
2. **Framework Preset**: Other.
3. **Root Directory**: leave as `./` (repo root).
4. Add these environment variables:

| Key | Value |
|---|---|
| `META_VERIFY_TOKEN` | any string you choose — must match the one you paste into the Meta dashboard |
| `META_ACCESS_TOKEN` | permanent token from Meta Business → System Users |
| `ANTHROPIC_API_KEY` | `sk-ant-…` |
| `OPENAI_API_KEY` | `sk-proj-…` |
| `SUPABASE_URL` | `https://xxx.supabase.co` |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Settings → API → `service_role` |

5. **Deploy**.

## Verify it's running

```bash
curl https://your-bot.vercel.app/health
# {"status":"ok"}
```

## Configure Meta WhatsApp webhook

Meta for Developers → your App → **WhatsApp → Configuration** → **Webhook**:

- **Callback URL**: `https://your-bot.vercel.app/whatsapp`
- **Verify token**: paste the same string you set as `META_VERIFY_TOKEN`
- Click **Verify and save** — Meta will GET the URL and the bot will echo back the challenge.
- Under **Webhook fields**, subscribe to **messages**.

## Local dev

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in real values
python app.py
# bot listens on http://localhost:5001
```

For local testing, expose via ngrok / cloudflared and paste the public URL
in Meta's webhook config.

## Routes

- `GET  /health` — health check
- `GET  /whatsapp` — Meta verification handshake
- `POST /whatsapp` — incoming WhatsApp messages (JSON)
- `POST /telegram/webhook` — incoming Telegram update (single master bot)
- `POST /telegram/admin/register-webhook` — one-time webhook setup (auth: `X-Sesly-Preview-Token`)

## Routing rules

- `/menu` — show onboarding, clear active session
- `/<handle>` — switch to that business's bot (e.g. `/alcipan`)
- `/<handle> <text>` — switch AND ask a question in one message
- Plain text — continue with the customer's currently active bot
- No active bot, no handle — send onboarding message

Voice notes are transcribed via OpenAI Whisper, then routed exactly like text.

## Telegram

Single master Telegram bot for the whole platform — exact same shape as
the single WhatsApp number. The bot's token lives in `TELEGRAM_BOT_TOKEN`
env var on sesly-bot, NOT in the database. Customers DM the master bot
and type `/<handle>` to switch between businesses (or click a deep link
like `t.me/<username>?start=<handle>` to land directly in a specific bot).

### Setup (one-time)

1. Create the bot in [@BotFather](https://t.me/BotFather) → get a token.
2. Add to sesly-bot Vercel env vars:
   - `TELEGRAM_BOT_TOKEN=<the token>`  (required)
   - `TELEGRAM_BOT_USERNAME=<username without @>`  (optional, skips a getMe call on each cold start)
   - `TELEGRAM_WEBHOOK_SECRET=<random 32-char string>`  (optional but recommended)
3. Redeploy sesly-bot.
4. Register the webhook once:
   ```bash
   curl -X POST "https://sesly-bot.vercel.app/telegram/admin/register-webhook" \
     -H "X-Sesly-Preview-Token: $SESLY_PREVIEW_TOKEN"
   ```
5. (Dashboard) set `NEXT_PUBLIC_TELEGRAM_BOT_USERNAME=<username>` on the dashboard Vercel project so the per-bot "Telegram" tab can show the share link.

### Customer identifier convention

- WhatsApp: `+994501234567` (E.164)
- Telegram: `tg:123456789` (Telegram `chat_id`)

Both formats are stored opaquely in `conversations.customer_phone` and
`bookings.customer_phone`.
