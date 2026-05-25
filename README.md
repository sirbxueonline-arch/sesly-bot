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

## Routing rules

- `/menu` — show onboarding, clear active session
- `/<handle>` — switch to that business's bot (e.g. `/alcipan`)
- `/<handle> <text>` — switch AND ask a question in one message
- Plain text — continue with the customer's currently active bot
- No active bot, no handle — send onboarding message

Voice notes are transcribed via OpenAI Whisper, then routed exactly like text.
