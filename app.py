"""
Sesly — multi-tenant WhatsApp webhook server (Meta Cloud API).

Meta sends:
- GET  /whatsapp — verification handshake (challenge / verify_token)
- POST /whatsapp — incoming messages as JSON

We reply by calling Graph API:
- POST https://graph.facebook.com/v20.0/<phone-number-id>/messages
"""
from __future__ import annotations
import os
import json
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

import db
import ai
import voice
from router import (
    MENU_MESSAGE,
    NO_BOT_MESSAGE,
    HANDLE_NOT_FOUND,
    parse_handle,
    get_remaining_message,
    is_menu_command,
)


def _away_response(bot: dict) -> str | None:
    """If the bot has 'away mode' active right now, return the away text.
    Otherwise None and we run the normal pipeline."""
    if not bot.get("away_enabled"):
        return None
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    starts = bot.get("away_starts_at")
    ends = bot.get("away_ends_at")
    try:
        if starts:
            s_dt = datetime.fromisoformat(starts.replace("Z", "+00:00"))
            if s_dt > now:
                return None
        if ends:
            e_dt = datetime.fromisoformat(ends.replace("Z", "+00:00"))
            if e_dt < now:
                return None
    except Exception as e:
        print(f"[away] could not parse window: {e}")
        return None
    msg = (bot.get("away_message") or "").strip()
    return (
        msg
        or "Salam! Bot hazırda tətildədir. Tezliklə qayıdacağıq 🌴"
    )

load_dotenv()

app = Flask(__name__)

GRAPH_API_VERSION = os.getenv("META_GRAPH_VERSION", "v20.0")
GRAPH_API = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


# ---------------------------------------------------------------------------
# Send / verify helpers
# ---------------------------------------------------------------------------

def _send_audio(phone_number_id: str, to: str, local_path: str) -> None:
    """Upload a local audio file to Meta, then send it as a WhatsApp audio msg."""
    token = os.getenv("META_ACCESS_TOKEN")
    if not token or not phone_number_id:
        return

    # 1) Upload to Meta media library
    try:
        with open(local_path, "rb") as f:
            up = requests.post(
                f"{GRAPH_API}/{phone_number_id}/media",
                headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp", "type": "audio/mpeg"},
                files={"file": ("reply.mp3", f, "audio/mpeg")},
                timeout=30,
            )
        if up.status_code >= 400:
            print(f"[audio] upload HTTP {up.status_code}: {up.text[:300]}")
            return
        media_id = up.json().get("id")
        if not media_id:
            print(f"[audio] upload returned no id: {up.text[:300]}")
            return
    except Exception as e:
        print(f"[audio] upload error: {e}")
        return

    # 2) Send the audio message
    try:
        r = requests.post(
            f"{GRAPH_API}/{phone_number_id}/messages",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "audio",
                "audio": {"id": media_id},
            },
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[audio] send HTTP {r.status_code}: {r.text[:300]}")
    except Exception as e:
        print(f"[audio] send error: {e}")
    finally:
        try:
            os.unlink(local_path)
        except Exception:
            pass


def _send_text(phone_number_id: str, to: str, body: str) -> None:
    """Send a WhatsApp text message via Meta Graph API."""
    token = os.getenv("META_ACCESS_TOKEN")
    if not token or not phone_number_id:
        print("[send] missing META_ACCESS_TOKEN or phone_number_id")
        return
    url = f"{GRAPH_API}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[send] HTTP {r.status_code}: {r.text}")
    except Exception as e:
        print(f"[send] error: {e}")


LIMIT_REACHED_MESSAGE = (
    "Üzr istəyirik 🙏 Bu ay üçün biznesin mesaj limiti dolub.\n"
    "Müştəri sizinlə birbaşa əlaqə saxlamalıdır.\n\n"
    "(Biznes sahibinə: planı yeniləyin — seslyai.com)"
)


def _handle_message(customer_phone: str, message: str, message_type: str = "text"):
    """Core routing logic. Returns (reply_text, bot_dict_or_None)."""
    handle = parse_handle(message)

    if is_menu_command(handle):
        db.clear_active_bot(customer_phone)
        return MENU_MESSAGE, None

    if handle:
        bot = db.get_bot_by_handle(handle)
        if not bot:
            return HANDLE_NOT_FOUND, None
        db.set_active_bot(customer_phone, bot["id"])

        away = _away_response(bot)
        if away:
            db.save_message(bot["id"], customer_phone, "user", message, message_type)
            db.save_message(bot["id"], customer_phone, "assistant", away)
            return away, bot

        extra = get_remaining_message(message)
        if extra:
            # Plan limit check (before saving anything, so we don't bloat counts)
            if db.is_over_message_limit(bot):
                return LIMIT_REACHED_MESSAGE, bot
            db.save_message(bot["id"], customer_phone, "user", extra, message_type)
            history = db.get_recent_history(bot["id"], customer_phone)
            if history and history[-1].get("content") == extra:
                history = history[:-1]
            reply, booking = ai.generate_reply_with_booking(bot, extra, history)
            db.save_message(bot["id"], customer_phone, "assistant", reply)
            if booking:
                db.save_booking(bot["id"], customer_phone, booking)
            return reply, bot

        greeting = bot.get("greeting_message") or "Salam! 👋"
        db.save_message(bot["id"], customer_phone, "assistant", greeting)
        return greeting, bot

    bot = db.get_active_bot(customer_phone)
    if not bot:
        # Fallback: if there's a default "sesly" sales bot, route to it.
        # This catches people who message the Sesly number directly without
        # typing a /handle first (e.g. clicking the landing-page CTA).
        bot = db.get_bot_by_handle("sesly")
        if bot:
            db.set_active_bot(customer_phone, bot["id"])
            print(f"[router] no handle, no session → falling back to /sesly")
        else:
            return NO_BOT_MESSAGE, None

    # Plan limit check
    if db.is_over_message_limit(bot):
        return LIMIT_REACHED_MESSAGE, bot

    away = _away_response(bot)
    if away:
        db.save_message(bot["id"], customer_phone, "user", message, message_type)
        db.save_message(bot["id"], customer_phone, "assistant", away)
        return away, bot

    db.save_message(bot["id"], customer_phone, "user", message, message_type)
    history = db.get_recent_history(bot["id"], customer_phone)
    if history and history[-1].get("content") == message:
        history = history[:-1]
    reply, booking = ai.generate_reply_with_booking(bot, message, history)
    db.save_message(bot["id"], customer_phone, "assistant", reply)
    if booking:
        db.save_booking(bot["id"], customer_phone, booking)
    return reply, bot


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return {"service": "sesly-bot", "status": "ok"}


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


@app.route("/debug", methods=["GET"])
def debug():
    """Diagnostic endpoint — confirms env + DB + Claude connectivity."""
    out = {
        "env": {
            "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
            "SUPABASE_SERVICE_ROLE_KEY_set": bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
            "SUPABASE_SERVICE_ROLE_KEY_starts": (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "")[:6],
            "META_ACCESS_TOKEN_set": bool(os.getenv("META_ACCESS_TOKEN")),
            "META_VERIFY_TOKEN_set": bool(os.getenv("META_VERIFY_TOKEN")),
            "ANTHROPIC_API_KEY_set": bool(os.getenv("ANTHROPIC_API_KEY")),
            "ANTHROPIC_API_KEY_starts": (os.getenv("ANTHROPIC_API_KEY") or "")[:10],
            "OPENAI_API_KEY_set": bool(os.getenv("OPENAI_API_KEY")),
        }
    }
    # Probe DB
    try:
        bots = db.client().table("bots").select("handle, is_active, display_name").execute()
        out["bots_count"] = len(bots.data or [])
        out["handles"] = [
            {"handle": b.get("handle"), "is_active": b.get("is_active"), "name": b.get("display_name")}
            for b in (bots.data or [])
        ][:20]
    except Exception as e:
        out["db_error"] = f"{type(e).__name__}: {e}"

    # Probe Claude
    try:
        c = ai.client()
        resp = c.messages.create(
            model=ai.MODEL,
            max_tokens=50,
            messages=[{"role": "user", "content": "Say 'ok' in one word."}],
        )
        out["claude_ok"] = True
        out["claude_reply"] = "".join(
            getattr(b, "text", "") for b in resp.content
        )[:120]
        out["claude_model"] = ai.MODEL
    except Exception as e:
        out["claude_error"] = f"{type(e).__name__}: {e}"
        out["claude_model_tried"] = ai.MODEL

    return out


@app.route("/cron/digest", methods=["GET", "POST"])
def cron_digest():
    """
    Daily owner digest — triggered by Vercel cron at 16:00 UTC (20:00 Baku).

    Auth: Vercel cron sends `Authorization: Bearer ${CRON_SECRET}`; we
    accept that, or alternatively a shared token via `X-Sesly-Cron-Token`
    for manual runs.
    """
    cron_secret = os.getenv("CRON_SECRET")
    fallback_token = os.getenv("SESLY_CRON_TOKEN")

    auth_ok = False
    auth_header = request.headers.get("Authorization", "")
    if cron_secret and auth_header == f"Bearer {cron_secret}":
        auth_ok = True
    elif fallback_token and request.headers.get("X-Sesly-Cron-Token") == fallback_token:
        auth_ok = True

    if not auth_ok:
        return jsonify({"error": "unauthorized"}), 401

    from digest import run_daily_digest
    try:
        result = run_daily_digest()
        return jsonify({"ok": True, **result})
    except Exception as e:
        print(f"[cron] digest failed: {type(e).__name__}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/preview", methods=["POST", "OPTIONS"])
def preview():
    """
    Dashboard preview endpoint — runs the bot's prompt against a test
    message WITHOUT touching WhatsApp or persisting the conversation.

    Auth: shared secret in `X-Sesly-Preview-Token` header
          (env: SESLY_PREVIEW_TOKEN).

    Body:
      {
        "bot_id":  "<uuid>",
        "message": "<user text>",
        "history": [{"role":"user|assistant","content":"..."}]  # optional
      }
    Returns: { "reply": "<text>" }
    """
    # Lightweight CORS preflight handling for browser-side fetches if needed
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Sesly-Preview-Token"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return resp

    expected = os.getenv("SESLY_PREVIEW_TOKEN")
    if not expected:
        return jsonify({"error": "preview_token_not_configured"}), 503
    if request.headers.get("X-Sesly-Preview-Token") != expected:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    bot_id = (payload.get("bot_id") or "").strip()
    message = (payload.get("message") or "").strip()
    history = payload.get("history") or []
    if not bot_id or not message:
        return jsonify({"error": "bot_id and message are required"}), 400

    bot = db.get_bot_by_id(bot_id)
    if not bot:
        return jsonify({"error": "bot_not_found"}), 404

    # Only keep valid history turns
    clean_history = []
    for m in history[-20:] if isinstance(history, list) else []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            clean_history.append({"role": role, "content": content})

    reply, booking = ai.generate_reply_with_booking(bot, message, clean_history)
    resp = jsonify({"reply": reply, "booking": booking})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


@app.route("/whatsapp", methods=["GET"])
def whatsapp_verify():
    """
    Meta webhook verification handshake.
    https://developers.facebook.com/docs/whatsapp/cloud-api/guides/set-up-webhooks
    """
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    expected = os.getenv("META_VERIFY_TOKEN", "")
    if mode == "subscribe" and token == expected and challenge:
        return challenge, 200
    return "Forbidden", 403


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Incoming WhatsApp message from Meta Cloud API."""
    data = request.get_json(silent=True) or {}

    try:
        entries = data.get("entry") or []
        for entry in entries:
            changes = entry.get("changes") or []
            for change in changes:
                value = change.get("value") or {}
                phone_number_id = (value.get("metadata") or {}).get("phone_number_id")
                messages = value.get("messages") or []
                for msg in messages:
                    _process_one_message(msg, phone_number_id)
    except Exception as e:
        print(f"[webhook] error: {e}")

    # Always 200 quickly so Meta doesn't retry
    return jsonify({"ok": True}), 200


def _process_one_message(msg: dict, phone_number_id: str | None) -> None:
    sender = (msg.get("from") or "").strip()
    mtype = msg.get("type")
    if not sender or not phone_number_id:
        return

    # Normalize — Meta sends digits-only ("994501234567"); we keep that format
    # for sending replies (Meta requires no +). For our DB key we add the +.
    reply_to = sender  # for Graph API replies, no leading +
    customer_phone = sender if sender.startswith("+") else f"+{sender}"
    print(f"[in] from={customer_phone} type={mtype}")

    body: str | None = None
    message_type = "text"

    if mtype == "text":
        body = ((msg.get("text") or {}).get("body") or "").strip()

    elif mtype == "audio":
        media_id = (msg.get("audio") or {}).get("id")
        if not media_id:
            _send_text(phone_number_id, reply_to, "Sesli mesajı oxuya bilmədim.")
            return
        transcript = voice.transcribe_meta_media(media_id)
        if not transcript:
            _send_text(
                phone_number_id, reply_to,
                "Sesli mesajınızı anlaya bilmədim, zəhmət olmasa yenidən cəhd edin və ya yazılı göndərin."
            )
            return
        print(f"[voice] transcript: {transcript!r}")
        body = transcript
        message_type = "voice"

    elif mtype == "interactive":
        it = msg.get("interactive") or {}
        kind = it.get("type")
        if kind == "button_reply":
            body = (it.get("button_reply") or {}).get("title")
        elif kind == "list_reply":
            body = (it.get("list_reply") or {}).get("title")
        body = (body or "").strip()

    elif mtype == "button":
        body = ((msg.get("button") or {}).get("text") or "").strip()

    else:
        _send_text(
            phone_number_id, reply_to,
            "Hələlik yalnız yazılı və sesli mesajları qəbul edirəm."
        )
        return

    if not body:
        _send_text(phone_number_id, reply_to, "Boş mesaj qəbul edildi. Zəhmət olmasa mətn yazın.")
        return

    reply, served_by = _handle_message(customer_phone, body, message_type=message_type)
    _send_text(phone_number_id, reply_to, reply)

    # Optional voice reply: only when the user spoke AND the bot is configured
    # for sesli cavablar AND TTS is configured. Best-effort; never blocks text.
    try:
        if (
            message_type == "voice"
            and served_by
            and served_by.get("voice_reply_enabled")
        ):
            import tts
            if tts.is_configured():
                audio_path = tts.synthesize(reply, served_by.get("voice_voice_id"))
                if audio_path:
                    _send_audio(phone_number_id, reply_to, audio_path)
    except Exception as e:
        print(f"[voice-reply] best-effort failed: {e}")


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
