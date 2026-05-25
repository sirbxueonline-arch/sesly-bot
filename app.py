"""
Sesly — multi-tenant WhatsApp webhook server.

Twilio POSTs incoming WhatsApp messages here. We:
1. Strip the `whatsapp:` prefix from the From number.
2. Parse a `/handle` if present.
3. Route to the right bot (or send onboarding / "not found").
4. Save the inbound + outbound messages.
5. Reply via TwiML.
"""
from __future__ import annotations
import os
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _twiml(text: str) -> Response:
    resp = MessagingResponse()
    resp.message(text)
    return Response(str(resp), mimetype="application/xml")


def _strip_whatsapp_prefix(phone: str) -> str:
    return (phone or "").replace("whatsapp:", "").strip()


def _handle_message(customer_phone: str, message: str, message_type: str = "text") -> str:
    """
    Core routing logic. Returns the reply text to send.
    Side-effect: saves inbound/outbound messages to DB.
    """
    handle = parse_handle(message)

    # /menu — show onboarding, clear session
    if is_menu_command(handle):
        db.clear_active_bot(customer_phone)
        return MENU_MESSAGE

    # /something — try to switch bots
    if handle:
        bot = db.get_bot_by_handle(handle)
        if not bot:
            return HANDLE_NOT_FOUND

        # Switch session
        db.set_active_bot(customer_phone, bot["id"])

        # If there was extra text after the handle, treat it as an actual question
        extra = get_remaining_message(message)
        if extra:
            # Save the user's question (without the handle), then reply
            db.save_message(bot["id"], customer_phone, "user", extra, message_type)
            history = db.get_recent_history(bot["id"], customer_phone)
            # Drop the just-saved message from history so it's not duplicated
            if history and history[-1].get("content") == extra:
                history = history[:-1]
            reply = ai.generate_reply(bot, extra, history)
            db.save_message(bot["id"], customer_phone, "assistant", reply)
            return reply

        # Otherwise just send the greeting and record it as the bot's first message
        greeting = bot.get("greeting_message") or "Salam! 👋"
        db.save_message(bot["id"], customer_phone, "assistant", greeting)
        return greeting

    # No handle — use active session
    bot = db.get_active_bot(customer_phone)
    if not bot:
        return NO_BOT_MESSAGE

    # Save the inbound message, generate AI reply
    db.save_message(bot["id"], customer_phone, "user", message, message_type)
    history = db.get_recent_history(bot["id"], customer_phone)
    if history and history[-1].get("content") == message:
        history = history[:-1]
    reply = ai.generate_reply(bot, message, history)
    db.save_message(bot["id"], customer_phone, "assistant", reply)
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


@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    from_number = _strip_whatsapp_prefix(request.values.get("From", ""))
    body = (request.values.get("Body") or "").strip()
    num_media = int(request.values.get("NumMedia") or 0)

    if not from_number:
        return _twiml("Bad request.")

    print(f"[in] from={from_number} body={body!r} media={num_media}")

    # Voice note path
    if num_media > 0:
        media_url = request.values.get("MediaUrl0")
        media_type = (request.values.get("MediaContentType0") or "").lower()
        if media_url and media_type.startswith("audio"):
            transcript = voice.transcribe_from_url(media_url)
            if not transcript:
                return _twiml(
                    "Sesli mesajınızı anlaya bilmədim, "
                    "zəhmət olmasa yenidən cəhd edin və ya yazılı göndərin."
                )
            print(f"[voice] transcript: {transcript!r}")
            reply = _handle_message(from_number, transcript, message_type="voice")
            return _twiml(reply)

    if not body:
        return _twiml("Boş mesaj qəbul edildi. Zəhmət olmasa mətn yazın.")

    reply = _handle_message(from_number, body, message_type="text")
    return _twiml(reply)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
