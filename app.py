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

load_dotenv()

app = Flask(__name__)

GRAPH_API_VERSION = os.getenv("META_GRAPH_VERSION", "v20.0")
GRAPH_API = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


# ---------------------------------------------------------------------------
# Send / verify helpers
# ---------------------------------------------------------------------------

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


def _handle_message(customer_phone: str, message: str, message_type: str = "text") -> str:
    """Core routing logic. Returns the reply text."""
    handle = parse_handle(message)

    if is_menu_command(handle):
        db.clear_active_bot(customer_phone)
        return MENU_MESSAGE

    if handle:
        bot = db.get_bot_by_handle(handle)
        if not bot:
            return HANDLE_NOT_FOUND
        db.set_active_bot(customer_phone, bot["id"])

        extra = get_remaining_message(message)
        if extra:
            # Plan limit check (before saving anything, so we don't bloat counts)
            if db.is_over_message_limit(bot):
                return LIMIT_REACHED_MESSAGE
            db.save_message(bot["id"], customer_phone, "user", extra, message_type)
            history = db.get_recent_history(bot["id"], customer_phone)
            if history and history[-1].get("content") == extra:
                history = history[:-1]
            reply, booking = ai.generate_reply_with_booking(bot, extra, history)
            db.save_message(bot["id"], customer_phone, "assistant", reply)
            if booking:
                db.save_booking(bot["id"], customer_phone, booking)
            return reply

        greeting = bot.get("greeting_message") or "Salam! 👋"
        db.save_message(bot["id"], customer_phone, "assistant", greeting)
        return greeting

    bot = db.get_active_bot(customer_phone)
    if not bot:
        return NO_BOT_MESSAGE

    # Plan limit check
    if db.is_over_message_limit(bot):
        return LIMIT_REACHED_MESSAGE

    db.save_message(bot["id"], customer_phone, "user", message, message_type)
    history = db.get_recent_history(bot["id"], customer_phone)
    if history and history[-1].get("content") == message:
        history = history[:-1]
    reply, booking = ai.generate_reply_with_booking(bot, message, history)
    db.save_message(bot["id"], customer_phone, "assistant", reply)
    if booking:
        db.save_booking(bot["id"], customer_phone, booking)
    return reply


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

    reply = _handle_message(customer_phone, body, message_type=message_type)
    _send_text(phone_number_id, reply_to, reply)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
