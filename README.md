# Sesly Bot — WhatsApp AI Webhook

Multi-tenant WhatsApp bot for Sesly. Receives Twilio webhooks, routes by `/handle`
to the correct bot, transcribes voice notes with Whisper, replies with Claude.

## Deploy to Vercel

1. **Vercel → Add New → Project** → import this repo.
2. **Framework Preset**: Other.
3. **Root Directory**: leave as `./` (repo root).
4. Add these environment variables:

| Key | Value |
|---|---|
| `TWILIO_ACCOUNT_SID` | from Twilio Console |
| `TWILIO_AUTH_TOKEN` | from Twilio Console |
| `TWILIO_WHATSAPP_NUMBER` | e.g. `whatsapp:+15559762340` |
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

## Point Twilio at it

Twilio Console → Phone Numbers → your number → Messaging Configuration →
**"A message comes in"** → `https://your-bot.vercel.app/whatsapp` (POST).

## Local dev

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in real values
python app.py
# bot listens on http://localhost:5001
```

Expose to Twilio with ngrok / cloudflared if testing locally.

## Routes

- `GET  /health` — health check
- `POST /whatsapp` — Twilio webhook (incoming WhatsApp messages)

## Routing rules

- `/menu` — show onboarding, clear active session
- `/<handle>` — switch to that business's bot (e.g. `/alcipan`)
- `/<handle> <text>` — switch AND ask a question in one message
- Plain text — continue with the customer's currently active bot
- No active bot, no handle — send onboarding message

Voice notes are transcribed via OpenAI Whisper, then routed exactly like text.
