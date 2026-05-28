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
import telegram as tg
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

def _send_audio(phone_number_id: str, to: str, local_path: str) -> dict:
    """Upload a local audio file to Meta, then send it as a WhatsApp audio msg.

    Returns a dict {ok: bool, error?: str, kind?: 'voice_note'|'attachment'}.
    Old callers can safely ignore the return value.

    Detects MIME from extension. OGG/Opus uploads are rendered by WhatsApp
    as voice notes (waveform UI). MP3 uploads render as audio attachments
    with a play button.
    """
    token = os.getenv("META_ACCESS_TOKEN")
    if not token or not phone_number_id:
        return {"ok": False, "error": "missing META_ACCESS_TOKEN or phone_number_id"}

    ext = os.path.splitext(local_path)[1].lower()
    if ext == ".ogg":
        mime = "audio/ogg"
        filename = "reply.ogg"
    elif ext in (".m4a", ".aac"):
        mime = "audio/mp4"
        filename = "reply.m4a"
    else:
        mime = "audio/mpeg"
        filename = "reply.mp3"

    # 1) Upload to Meta media library
    try:
        with open(local_path, "rb") as f:
            up = requests.post(
                f"{GRAPH_API}/{phone_number_id}/media",
                headers={"Authorization": f"Bearer {token}"},
                data={"messaging_product": "whatsapp", "type": mime},
                files={"file": (filename, f, mime)},
                timeout=30,
            )
        if up.status_code >= 400:
            msg = f"meta upload {up.status_code}: {up.text[:300]}"
            print(f"[audio] {msg}")
            return {"ok": False, "error": msg}
        media_id = up.json().get("id")
        if not media_id:
            msg = f"meta upload no id: {up.text[:300]}"
            print(f"[audio] {msg}")
            return {"ok": False, "error": msg}
        print(f"[audio] uploaded media={media_id} mime={mime}")
    except Exception as e:
        print(f"[audio] upload error: {e}")
        return {"ok": False, "error": f"upload exception: {e}"}

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
            msg = f"meta send {r.status_code}: {r.text[:300]}"
            print(f"[audio] {msg}")
            return {"ok": False, "error": msg}
        kind = "voice_note" if ext == ".ogg" else "attachment"
        print(f"[audio] sent to {to} (rendered as {kind.replace('_', ' ')})")
        return {"ok": True, "kind": kind}
    except Exception as e:
        print(f"[audio] send error: {e}")
        return {"ok": False, "error": f"send exception: {e}"}
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
            # Enrich bot dict with staff list + customer history for AI context
            bot["_staff"] = db.get_bot_staff(bot["id"])
            bot["_customer_context"] = db.get_customer_history(bot["id"], customer_phone)
            history = db.get_recent_history(bot["id"], customer_phone)
            if history and history[-1].get("content") == extra:
                history = history[:-1]
            reply, booking, wants_cancel = ai.generate_reply_with_booking(bot, extra, history)
            db.save_message(bot["id"], customer_phone, "assistant", reply)
            if booking:
                db.save_booking(bot["id"], customer_phone, booking)
            if wants_cancel:
                try:
                    db.cancel_latest_booking(bot["id"], customer_phone)
                except Exception as e:
                    print(f"[handler] AI-cancel failed: {e}")
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

    # Customer rating detection — if the message is just "1"-"5" (or stars),
    # save as a review and reply with a thank-you instead of running AI.
    rating = db.detect_and_save_review(bot["id"], customer_phone, message)
    if rating:
        thank_you = (
            "Təşəkkür edirik 💛 Rəyiniz qeyd olundu — biznesə kömək edir."
            if rating >= 4
            else "Təşəkkür edirik rəyiniz üçün 🙏 Yaxşılaşdırmaq üçün çalışacağıq."
        )
        db.save_message(bot["id"], customer_phone, "user", message, message_type)
        db.save_message(bot["id"], customer_phone, "assistant", thank_you)
        return thank_you, bot

    # Customer-initiated cancellation — "ləğv et" / "iptal" / "cancel" /
    # "gəlməyəcəm" → cancel their most recent pending or confirmed booking.
    if db.detect_cancellation_intent(message):
        cancelled = db.cancel_latest_booking(bot["id"], customer_phone)
        if cancelled:
            when = cancelled.get("scheduled_time_text") or cancelled.get("scheduled_at") or ""
            service = cancelled.get("service") or "Randevu"
            reply = (
                f"Anlaşıldı, randevunuz ləğv edildi 🗓\n\n"
                f"{service}{(' — ' + when) if when else ''}\n\n"
                "Başqa vaxta qeyd etmək istəsəniz, yazın — sizə yer açım."
            )
            db.save_message(bot["id"], customer_phone, "user", message, message_type)
            db.save_message(bot["id"], customer_phone, "assistant", reply)
            return reply, bot
        # No active booking found — fall through to AI which can clarify

    away = _away_response(bot)
    if away:
        db.save_message(bot["id"], customer_phone, "user", message, message_type)
        db.save_message(bot["id"], customer_phone, "assistant", away)
        return away, bot

    db.save_message(bot["id"], customer_phone, "user", message, message_type)

    # Enrich bot dict with staff list + customer history for the system prompt.
    # ai.py reads bot['_staff'] and bot['_customer_context'].
    bot["_staff"] = db.get_bot_staff(bot["id"])
    bot["_customer_context"] = db.get_customer_history(bot["id"], customer_phone)

    history = db.get_recent_history(bot["id"], customer_phone)
    if history and history[-1].get("content") == message:
        history = history[:-1]
    reply, booking, wants_cancel = ai.generate_reply_with_booking(bot, message, history)
    db.save_message(bot["id"], customer_phone, "assistant", reply)
    if booking:
        db.save_booking(bot["id"], customer_phone, booking)
    # AI-detected cancellation (covers cases the keyword pre-check missed —
    # e.g. "ola bilmir, başqa vaxt yazaram" or "не приду извините")
    if wants_cancel:
        try:
            cancelled = db.cancel_latest_booking(bot["id"], customer_phone)
            if cancelled:
                print(f"[handler] AI-cancel succeeded for booking={cancelled.get('id')}")
            else:
                print("[handler] AI flagged cancel but no active booking found")
        except Exception as e:
            print(f"[handler] AI-cancel failed: {e}")
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
    """Diagnostic endpoint — confirms env + DB + OpenAI + voice pipeline."""
    eleven_key = os.getenv("ELEVENLABS_API_KEY") or ""
    out = {
        "env": {
            "SUPABASE_URL": bool(os.getenv("SUPABASE_URL")),
            "SUPABASE_SERVICE_ROLE_KEY_set": bool(os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
            "SUPABASE_SERVICE_ROLE_KEY_starts": (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "")[:6],
            "META_ACCESS_TOKEN_set": bool(os.getenv("META_ACCESS_TOKEN")),
            "META_VERIFY_TOKEN_set": bool(os.getenv("META_VERIFY_TOKEN")),
            "META_PHONE_NUMBER_ID_set": bool(os.getenv("META_PHONE_NUMBER_ID")),
            "OPENAI_API_KEY_set": bool(os.getenv("OPENAI_API_KEY")),
            "ELEVENLABS_API_KEY_set": bool(eleven_key),
            "ELEVENLABS_API_KEY_starts": eleven_key[:6],
            "ELEVENLABS_DEFAULT_VOICE": os.getenv("ELEVENLABS_DEFAULT_VOICE") or None,
            "SESLY_PREVIEW_TOKEN_set": bool(os.getenv("SESLY_PREVIEW_TOKEN")),
            "AI_MODEL": ai.MODEL,
        }
    }
    # Probe DB
    try:
        bots = db.client().table("bots").select(
            "handle, is_active, display_name, voice_reply_enabled, voice_voice_id"
        ).execute()
        out["bots_count"] = len(bots.data or [])
        out["handles"] = [
            {
                "handle": b.get("handle"),
                "is_active": b.get("is_active"),
                "name": b.get("display_name"),
                "voice_reply_enabled": b.get("voice_reply_enabled"),
                "voice_voice_id": b.get("voice_voice_id"),
            }
            for b in (bots.data or [])
        ][:20]
    except Exception as e:
        out["db_error"] = f"{type(e).__name__}: {e}"

    # Probe OpenAI chat completions
    try:
        c = ai.client()
        resp = c.chat.completions.create(
            model=ai.MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with the word 'ok' only."}],
        )
        out["openai_ok"] = True
        out["openai_reply"] = (resp.choices[0].message.content or "")[:120] if resp.choices else ""
        out["openai_model"] = ai.MODEL
    except Exception as e:
        out["openai_error"] = f"{type(e).__name__}: {e}"
        out["openai_model_tried"] = ai.MODEL

    # Probe ffmpeg availability (needed for OGG/Opus conversion → voice notes)
    try:
        import tts
        ff = tts._ffmpeg_path()
        out["ffmpeg_available"] = bool(ff)
        out["ffmpeg_path"] = ff if ff else None
    except Exception as e:
        out["ffmpeg_error"] = f"{type(e).__name__}: {e}"

    # Probe ElevenLabs — tiny synth to see if the key is valid AND has quota
    if eleven_key:
        try:
            import tts
            voice_id = os.getenv("ELEVENLABS_DEFAULT_VOICE") or tts.DEFAULT_VOICE_ID
            import requests
            r = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": eleven_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": "ok",
                    "model_id": "eleven_multilingual_v2",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=15,
            )
            out["elevenlabs_status"] = r.status_code
            if r.status_code != 200:
                out["elevenlabs_error"] = r.text[:400]
            else:
                out["elevenlabs_ok"] = True
                out["elevenlabs_bytes"] = len(r.content)
                out["elevenlabs_voice_id"] = voice_id
        except Exception as e:
            out["elevenlabs_exception"] = f"{type(e).__name__}: {e}"
    else:
        out["elevenlabs_skipped"] = "ELEVENLABS_API_KEY not set"

    return out


@app.route("/voice-test", methods=["POST", "OPTIONS"])
def voice_test():
    """
    Run the full TTS → WhatsApp voice-note pipeline once and return a
    detailed report. Surfaces the exact reason if anything fails so the
    dashboard can show it to the owner.

    Auth: shared `X-Sesly-Preview-Token` (same secret used by /preview).

    Body: {bot_id, to_phone, text?}
    Returns: {
      ok: bool,
      stages: { config, db_lookup, synthesize, send },
      voice_id, text_synthesized, error?
    }
    """
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Sesly-Preview-Token"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return resp

    expected = os.getenv("SESLY_PREVIEW_TOKEN")
    if not expected:
        return jsonify({"ok": False, "stage": "auth", "error": "preview_token_not_configured"}), 503
    if request.headers.get("X-Sesly-Preview-Token") != expected:
        return jsonify({"ok": False, "stage": "auth", "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    bot_id = (payload.get("bot_id") or "").strip()
    to_phone = (payload.get("to_phone") or "").strip()
    text = (payload.get("text") or "Salam! Bu Sesly səsli cavab testidir.").strip()
    if not bot_id or not to_phone:
        return jsonify({"ok": False, "stage": "input", "error": "bot_id and to_phone required"}), 400

    report = {
        "ok": False,
        "bot_id": bot_id,
        "to_phone": to_phone,
        "text_synthesized": text,
        "voice_id": None,
        "stages": {},
    }

    # Stage 1: env config
    import tts
    api_key_set = bool(os.getenv("ELEVENLABS_API_KEY"))
    default_voice = os.getenv("ELEVENLABS_DEFAULT_VOICE")
    meta_token = bool(os.getenv("META_ACCESS_TOKEN"))
    meta_phone_id = os.getenv("META_PHONE_NUMBER_ID")
    report["stages"]["config"] = {
        "elevenlabs_api_key": api_key_set,
        "elevenlabs_default_voice": default_voice or None,
        "meta_access_token": meta_token,
        "meta_phone_number_id": bool(meta_phone_id),
    }
    if not api_key_set:
        report["error"] = "ELEVENLABS_API_KEY not set on sesly-bot Vercel project"
        return jsonify(report), 200
    if not meta_token or not meta_phone_id:
        report["error"] = "META_ACCESS_TOKEN / META_PHONE_NUMBER_ID not set on sesly-bot"
        return jsonify(report), 200

    # Stage 2: DB lookup (so we surface migration 008 missing as a real error)
    try:
        bot_row = db.get_bot_by_id(bot_id)
    except Exception as e:
        report["stages"]["db_lookup"] = {"ok": False, "error": str(e)}
        report["error"] = f"db lookup failed: {e}"
        return jsonify(report), 200
    if not bot_row:
        report["stages"]["db_lookup"] = {"ok": False, "error": "bot not found"}
        report["error"] = "Bot tapılmadı"
        return jsonify(report), 200
    voice_reply_enabled_in_db = bot_row.get("voice_reply_enabled")
    voice_voice_id = bot_row.get("voice_voice_id")
    report["stages"]["db_lookup"] = {
        "ok": True,
        "voice_reply_enabled": voice_reply_enabled_in_db,
        "voice_voice_id": voice_voice_id,
        "voice_reply_enabled_column_exists": voice_reply_enabled_in_db is not None
            or "voice_reply_enabled" in bot_row,
    }
    if "voice_reply_enabled" not in bot_row:
        report["error"] = (
            "Migration 008 hələ tətbiq edilməyib (voice_reply_enabled sütunu yoxdur). "
            "Supabase SQL Editor → run sesly/supabase/migrations/008_integrations_scaffold.sql"
        )
        return jsonify(report), 200

    chosen_voice = voice_voice_id or default_voice or tts.DEFAULT_VOICE_ID
    report["voice_id"] = chosen_voice

    # Stage 3: synthesize
    try:
        audio_path = tts.synthesize(text, chosen_voice)
    except Exception as e:
        report["stages"]["synthesize"] = {"ok": False, "error": str(e)}
        report["error"] = f"synthesize exception: {e}"
        return jsonify(report), 200
    if not audio_path:
        report["stages"]["synthesize"] = {"ok": False, "error": "synthesize returned None — check ELEVENLABS_API_KEY validity or quota"}
        report["error"] = "ElevenLabs synthesis failed (check logs for HTTP code)"
        return jsonify(report), 200
    report["stages"]["synthesize"] = {
        "ok": True,
        "format": "ogg/opus" if audio_path.endswith(".ogg") else "mp3 (no ffmpeg)",
    }

    # Stage 4: send to WhatsApp
    send_result = _send_audio(meta_phone_id, to_phone, audio_path)
    report["stages"]["send"] = send_result
    if not send_result.get("ok"):
        report["error"] = send_result.get("error") or "WhatsApp send failed"
        return jsonify(report), 200

    report["ok"] = True
    report["message"] = (
        "Səsli cavab göndərildi 🎉 WhatsApp-da yoxlayın. "
        f"({'voice note' if send_result.get('kind') == 'voice_note' else 'audio attachment'})"
    )
    return jsonify(report), 200


@app.route("/send-message", methods=["POST", "OPTIONS"])
def send_message():
    """
    Send an arbitrary WhatsApp text from a bot to a customer.

    Used by the dashboard when the owner marks a booking complete — the
    bot then asks the customer for a 1-5 star rating.

    Auth: shared X-Sesly-Preview-Token (same secret as /preview).

    Body: {bot_id, customer_phone, message}
    Returns: {ok: bool, error?: str}
    """
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Sesly-Preview-Token"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return resp

    expected = os.getenv("SESLY_PREVIEW_TOKEN")
    if not expected:
        return jsonify({"ok": False, "error": "preview_token_not_configured"}), 503
    if request.headers.get("X-Sesly-Preview-Token") != expected:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    bot_id = (payload.get("bot_id") or "").strip()
    customer_phone = (payload.get("customer_phone") or "").strip()
    message = (payload.get("message") or "").strip()

    if not customer_phone or not message:
        return jsonify({"ok": False, "error": "customer_phone and message required"}), 400

    # Telegram path — uses TELEGRAM_BOT_TOKEN env var (single master bot)
    if tg.is_telegram_customer(customer_phone):
        out = _send_to_customer({}, customer_phone, message)
        if not out.get("ok"):
            return jsonify({"ok": False, "error": out.get("error")}), 502
        # Log into history if we know which bot owns this conversation
        if bot_id:
            try:
                db.save_message(bot_id, customer_phone, "assistant", message)
            except Exception as e:
                print(f"[send-message] log save failed: {e}")
        return jsonify({"ok": True, "channel": "telegram"})

    # WhatsApp path (default)
    digits = "".join(c for c in customer_phone if c.isdigit())
    if len(digits) < 8:
        return jsonify({"ok": False, "error": "invalid_phone"}), 400

    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
    if not phone_number_id:
        return jsonify({"ok": False, "error": "META_PHONE_NUMBER_ID not set"}), 503

    try:
        _send_text(phone_number_id, digits, message)
    except Exception as e:
        return jsonify({"ok": False, "error": f"send_error: {e}"}), 502

    # Log into the conversation history so the bot remembers what it sent
    if bot_id:
        try:
            store_phone = customer_phone if customer_phone.startswith("+") else f"+{customer_phone}"
            db.save_message(bot_id, store_phone, "assistant", message)
        except Exception as e:
            print(f"[send-message] log save failed: {e}")

    return jsonify({"ok": True, "channel": "whatsapp"})


# ---------------------------------------------------------------------------
# Telegram bot integration (single master bot, env-configured — same shape
# as the WhatsApp setup. ONE token in TELEGRAM_BOT_TOKEN env var, ONE
# webhook, customers type /handle to switch between businesses.)
# ---------------------------------------------------------------------------

def _telegram_token() -> str | None:
    return os.getenv("TELEGRAM_BOT_TOKEN")


def _telegram_bot_username() -> str:
    """Cached username of the master bot. Falls back to env var override,
    otherwise looks it up via getMe (cheap, one-time)."""
    cached = getattr(_telegram_bot_username, "_cached", None)
    if cached:
        return cached
    override = os.getenv("TELEGRAM_BOT_USERNAME")
    if override:
        _telegram_bot_username._cached = override
        return override
    token = _telegram_token()
    if not token:
        return ""
    me = tg.get_me(token)
    if me.get("ok") and me.get("username"):
        _telegram_bot_username._cached = me["username"]
        return me["username"]
    return ""


def _send_to_customer(bot: dict, customer_phone: str, text: str) -> dict:
    """Route an outgoing message based on the customer_phone prefix.

    - "tg:<chat_id>"  → Telegram (uses TELEGRAM_BOT_TOKEN env var — single
                         master bot for the whole platform, same shape as
                         the META_PHONE_NUMBER_ID setup for WhatsApp)
    - "+994..."       → WhatsApp (uses META_PHONE_NUMBER_ID)
    Returns {ok: bool, error?: str, channel: 'telegram' | 'whatsapp'}.
    The `bot` arg is kept for signature parity; not used for Telegram.
    """
    if tg.is_telegram_customer(customer_phone):
        chat_id = tg.customer_phone_to_chat_id(customer_phone)
        token = _telegram_token()
        if not token or not chat_id:
            return {
                "ok": False,
                "error": "TELEGRAM_BOT_TOKEN not set",
                "channel": "telegram",
            }
        out = tg.send_message(token, chat_id, text)
        out["channel"] = "telegram"
        return out
    # WhatsApp
    digits = "".join(c for c in customer_phone if c.isdigit())
    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
    if not digits or not phone_number_id:
        return {"ok": False, "error": "no whatsapp config or invalid phone", "channel": "whatsapp"}
    try:
        _send_text(phone_number_id, digits, text)
        return {"ok": True, "channel": "whatsapp"}
    except Exception as e:
        return {"ok": False, "error": str(e), "channel": "whatsapp"}


@app.route("/telegram/status", methods=["GET", "POST", "OPTIONS"])
@app.route("/telegram/admin/status", methods=["GET", "POST", "OPTIONS"])
def telegram_admin_status():
    """Diagnostic: report exactly why Telegram isn't responding. Public —
    nothing returned is sensitive (bot username, webhook URL, error
    messages are all info Telegram itself returns publicly). The token
    value is masked to first 8 chars only.

    Returns: { token_set, token_starts, getMe, webhookInfo, expected_url,
              diagnosis }
    """
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    token = _telegram_token()
    base_url = (
        os.getenv("PUBLIC_BASE_URL") or "https://sesly-bot.vercel.app"
    ).rstrip("/")
    expected_url = f"{base_url}/telegram/webhook"

    out: dict = {
        "token_set": bool(token),
        "token_starts": (token[:8] + "…") if token else None,
        "expected_webhook_url": expected_url,
        "TELEGRAM_BOT_USERNAME_env": os.getenv("TELEGRAM_BOT_USERNAME") or None,
        "TELEGRAM_WEBHOOK_SECRET_set": bool(os.getenv("TELEGRAM_WEBHOOK_SECRET")),
    }

    if not token:
        out["next_step"] = (
            "Set TELEGRAM_BOT_TOKEN env var on the sesly-bot Vercel "
            "project, redeploy, then call /telegram/admin/register-webhook."
        )
        return jsonify(out)

    # Probe the token
    me = tg.get_me(token)
    out["getMe"] = me

    # Probe what webhook Telegram currently has registered
    try:
        import requests
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getWebhookInfo",
            timeout=10,
        )
        info = r.json()
        if info.get("ok"):
            result = info.get("result") or {}
            out["webhookInfo"] = {
                "current_url": result.get("url"),
                "has_custom_certificate": result.get("has_custom_certificate"),
                "pending_update_count": result.get("pending_update_count"),
                "last_error_date": result.get("last_error_date"),
                "last_error_message": result.get("last_error_message"),
                "max_connections": result.get("max_connections"),
                "ip_address": result.get("ip_address"),
            }
            # Diagnose
            current = result.get("url") or ""
            if not current:
                out["diagnosis"] = (
                    "Webhook is NOT registered with Telegram. Call POST "
                    "/telegram/admin/register-webhook to register it."
                )
            elif current != expected_url:
                out["diagnosis"] = (
                    f"Webhook points at {current!r} but we expected {expected_url!r}. "
                    f"Call /telegram/admin/register-webhook to fix."
                )
            elif result.get("last_error_message"):
                out["diagnosis"] = (
                    f"Webhook URL is correct but Telegram is getting an error: "
                    f"{result.get('last_error_message')!r}. Check sesly-bot logs."
                )
            else:
                out["diagnosis"] = "Looks healthy. If still no replies, check sesly-bot Vercel logs."
        else:
            out["webhookInfo_error"] = info
    except Exception as e:
        out["webhookInfo_error"] = f"{type(e).__name__}: {e}"

    return jsonify(out)


@app.route("/telegram/register-webhook", methods=["GET", "POST", "OPTIONS"])
@app.route("/telegram/admin/register-webhook", methods=["GET", "POST", "OPTIONS"])
def telegram_admin_register_webhook():
    """One-time webhook registration. Call this once after setting
    TELEGRAM_BOT_TOKEN in Vercel env vars to point Telegram at our
    /telegram/webhook endpoint.

    This endpoint is intentionally public — the worst an attacker could do
    is force-rebind Telegram to our OWN webhook (the URL is derived from
    PUBLIC_BASE_URL env). They can't redirect it elsewhere without our
    bot token, which is server-side only.

    GET/POST both work, so you can just open the URL in a browser.
    Returns: { ok, webhook_url, username, error? }
    """
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    token = _telegram_token()
    if not token:
        return jsonify({
            "ok": False,
            "error": "TELEGRAM_BOT_TOKEN env var not set on sesly-bot",
        }), 503

    payload = request.get_json(silent=True) or {}
    base_url = (
        payload.get("public_base_url")
        or os.getenv("PUBLIC_BASE_URL")
        or "https://sesly-bot.vercel.app"
    ).rstrip("/")
    webhook_url = f"{base_url}/telegram/webhook"
    webhook_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")

    # Validate the token by calling getMe — this also gives us the username
    me = tg.get_me(token)
    if not me.get("ok"):
        return jsonify({
            "ok": False,
            "error": f"invalid_token: {me.get('error')}",
        }), 400

    set_result = tg.set_webhook(token, webhook_url, secret=webhook_secret)
    if not set_result.get("ok"):
        return jsonify({
            "ok": False,
            "error": f"webhook_failed: {set_result.get('error')}",
        }), 502

    # Cache the username so we don't have to re-fetch on every webhook
    _telegram_bot_username._cached = me.get("username")

    return jsonify({
        "ok": True,
        "webhook_url": webhook_url,
        "username": me.get("username"),
        "telegram_id": me.get("id"),
    })


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Receive an incoming Telegram update on the master bot (single
    webhook, env-configured — same shape as /whatsapp)."""
    # Optional shared-secret check (Telegram echoes back our secret_token
    # in this header on every update)
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if expected_secret:
        if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != expected_secret:
            return jsonify({"ok": False, "error": "bad_secret"}), 401

    update = request.get_json(silent=True) or {}

    # Idempotency guard. Telegram retries webhook deliveries if we're slow
    # (cold start + OpenAI call can easily push past Telegram's timeout),
    # which would otherwise produce duplicate replies.
    update_id = update.get("update_id")
    if update_id and db.telegram_update_seen(update_id):
        print(f"[telegram] skipping duplicate update_id={update_id}")
        return jsonify({"ok": True, "skipped": "duplicate"}), 200

    try:
        _process_telegram_update(update)
    except Exception as e:
        print(f"[telegram] webhook error: {e}")

    # Always 200 quickly so Telegram doesn't retry
    return jsonify({"ok": True}), 200


def _process_telegram_update(update: dict) -> None:
    """Run a Telegram update through the same _handle_message pipeline that
    WhatsApp uses. Single master bot — customers /handle to switch.

    Routing model (mirrors WhatsApp exactly):
      - "/start"                       → falls through to /sesly (master
                                         sales bot) via _handle_message's
                                         built-in fallback.
      - "/start <handle>" (deep link)  → routed to "/<handle>" so links like
                                         t.me/seslyaibot?start=aysel_salon
                                         drop customers directly into Aysel.
      - "/<handle>"                    → switches active bot
      - "/menu"                        → shows the menu
      - plain text / voice             → continues with the active bot, or
                                         /sesly fallback for new customers
    """
    msg = update.get("message") or update.get("edited_message") or {}
    if not msg:
        return

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    # Skip group / channel / supergroup chats — Sesly is 1:1 only
    if chat.get("type") not in (None, "private"):
        return

    token = _telegram_token()
    if not token:
        print("[telegram] TELEGRAM_BOT_TOKEN not set; dropping update")
        return

    customer_phone = tg.chat_id_to_customer_phone(chat_id)
    print(f"[tg-in] chat={chat_id}")

    body: str | None = None
    message_type = "text"
    bot_username = _telegram_bot_username()
    # Track whether this update is a /start so we can offer the contact
    # share button AFTER the bot's greeting.
    is_start_command = False

    # --- Contact share message ----------------------------------------------
    # When the customer taps the "Share phone" button, Telegram sends a
    # message with a `contact` field. We save it, acknowledge, and stop
    # before running the AI pipeline (this isn't a chat message).
    if "contact" in msg:
        contact = msg.get("contact") or {}
        from_user = msg.get("from") or {}
        # Only accept the contact if it's the SAME user sharing their OWN
        # number (Telegram lets you forward someone else's contact too,
        # which we don't want as the customer's identity).
        if contact.get("user_id") and contact.get("user_id") == from_user.get("id"):
            phone = (contact.get("phone_number") or "").strip()
            # Telegram returns the phone without a leading "+" — add it for
            # consistency with the WhatsApp E.164 format.
            if phone and not phone.startswith("+"):
                phone = "+" + phone
            first_name = contact.get("first_name") or from_user.get("first_name")
            last_name = contact.get("last_name") or from_user.get("last_name")
            db.save_telegram_contact(
                chat_id,
                phone=phone or None,
                first_name=first_name,
                last_name=last_name,
                username=from_user.get("username"),
                language_code=from_user.get("language_code"),
            )
            # If Telegram didn't include a surname (most accounts only set
            # a first name), follow up and ask for it explicitly so the
            # owner has the full name in the dashboard / on bookings.
            if first_name and not last_name:
                db.set_telegram_awaiting_surname(chat_id, True)
                tg.send_message(
                    token, chat_id,
                    f"Təşəkkürlər, {first_name}! 🙏 Soyadınızı da yazardınız? "
                    "Biznes sahibi sizinlə əlaqə saxlamaq lazım olduqda "
                    "tam adınızı görəcək.",
                    reply_markup=tg.remove_keyboard(),
                )
            else:
                tg.send_message(
                    token, chat_id,
                    f"Təşəkkürlər! Nömrəniz qeyd olundu ({phone}). İndi sualınızı yazın.",
                    reply_markup=tg.remove_keyboard(),
                )
        else:
            tg.send_message(
                token, chat_id,
                "Yalnız öz nömrənizi paylaşa bilərsiniz. 🙂",
                reply_markup=tg.remove_keyboard(),
            )
        return

    if "text" in msg:
        body = (msg.get("text") or "").strip()

        # Normalize Telegram-specific commands into WhatsApp-style /handle
        # so _handle_message can route them.
        if body == "/start" or (bot_username and body == f"/start@{bot_username}"):
            is_start_command = True
            body = ""
        elif body.startswith("/start "):
            # Deep link: /start <payload> → /<payload>
            is_start_command = True
            payload = body[7:].strip().split()[0] if body[7:].strip() else ""
            body = f"/{payload}" if payload else ""
        elif body.startswith("/start@") and " " in body:
            # /start@seslyaibot aysel_salon — same deep link, group-style
            is_start_command = True
            tail = body.split(" ", 1)[1].strip().split()[0]
            body = f"/{tail}" if tail else ""

    elif "voice" in msg or "audio" in msg:
        vfile = (msg.get("voice") or msg.get("audio") or {})
        file_id = vfile.get("file_id")
        if not file_id:
            tg.send_message(token, chat_id, "Sesli mesajı oxuya bilmədim.")
            return
        transcript = tg.transcribe_voice(token, file_id)
        if not transcript:
            tg.send_message(
                token, chat_id,
                "Sesli mesajınızı anlaya bilmədim, zəhmət olmasa yenidən cəhd edin və ya yazılı göndərin."
            )
            return
        print(f"[tg-voice] transcript: {transcript!r}")
        body = transcript
        message_type = "voice"

    elif "photo" in msg or "document" in msg or "video" in msg or "sticker" in msg:
        tg.send_message(
            token, chat_id,
            "Hələlik yalnız yazılı və sesli mesajları qəbul edirəm."
        )
        return

    else:
        tg.send_message(
            token, chat_id,
            "Hələlik yalnız yazılı və sesli mesajları qəbul edirəm."
        )
        return

    # Empty body (bare /start after normalization) → run through
    # _handle_message with an empty string. The router will fall through to
    # /sesly via its built-in fallback, exactly like a new WhatsApp number
    # texting in cold.
    if body is None:
        return

    # If we asked this customer for their surname after they shared their
    # contact, the next message (this one, if it's plain text) is the
    # surname. Save it and ack — don't run it through the AI.
    if message_type == "text" and body and not body.startswith("/"):
        try:
            existing_contact = db.get_telegram_contact(chat_id)
            if existing_contact and existing_contact.get("awaiting_surname"):
                surname = body.strip()
                # Sanity-check: only accept short, name-shaped input. Anything
                # too long or containing question words probably means the
                # customer ignored the prompt and is actually asking something.
                looks_like_name = (
                    len(surname) <= 60
                    and "?" not in surname
                    and not surname.lower().startswith(("salam", "necə", "qiymət", "nə ", "/"))
                )
                if looks_like_name:
                    db.save_telegram_contact(
                        chat_id,
                        phone=None,  # don't overwrite existing phone
                        last_name=surname,
                        awaiting_surname=False,
                    )
                    first = existing_contact.get("first_name") or ""
                    full = f"{first} {surname}".strip()
                    tg.send_message(
                        token, chat_id,
                        f"Təşəkkürlər, {full}! ✅ İndi sualınızı yazın.",
                    )
                    return
                # Customer ignored the prompt — clear flag, let message flow
                db.set_telegram_awaiting_surname(chat_id, False)
        except Exception as e:
            print(f"[tg-surname] check failed: {e}")

    reply, served_by = _handle_message(customer_phone, body, message_type=message_type)

    # Voice replies on Telegram follow the same kill switch / per-bot toggle
    voice_replies_disabled = os.getenv("VOICE_REPLIES_DISABLED", "").lower() in ("1", "true", "yes")
    should_voice = (
        not voice_replies_disabled
        and message_type == "voice"
        and served_by
        and served_by.get("voice_reply_enabled")
    )

    voice_sent = False
    if should_voice:
        try:
            import tts
            if tts.is_configured():
                voice_id = served_by.get("voice_voice_id")
                audio_path = tts.synthesize(reply, voice_id)
                if audio_path:
                    send_result = tg.send_voice(token, chat_id, audio_path)
                    if send_result.get("ok"):
                        voice_sent = True
                    else:
                        print(f"[tg-voice-reply] failed: {send_result.get('error')}")
        except Exception as e:
            print(f"[tg-voice-reply] exception: {e}")

    if not voice_sent:
        tg.send_message(token, chat_id, reply)

    # After the bot's /start greeting, prompt the customer to share their
    # phone — but only if we don't already have it. The dashboard joins on
    # telegram_contacts to show real phone numbers next to Telegram
    # conversations instead of useless masked chat_ids.
    if is_start_command:
        try:
            if not db.has_telegram_contact(chat_id):
                tg.send_message(
                    token,
                    chat_id,
                    (
                        "📞 Müştəri xidmətimiz üçün ad, soyad və telefon "
                        "nömrənizi paylaşa bilərsiniz — biznes lazım olduqda "
                        "sizinlə əlaqə saxlamaq üçün istifadə edəcək. "
                        "Düyməyə basın, biz qalanını həll edirik."
                    ),
                    reply_markup=tg.contact_request_keyboard(),
                )
        except Exception as e:
            print(f"[tg-contact] prompt failed: {e}")


@app.route("/cron/hourly", methods=["GET", "POST"])
def cron_hourly():
    """Combined hourly cron — runs reminders + auto-complete in one call.
    Authenticated via CRON_SECRET (Vercel cron) or SESLY_CRON_TOKEN."""
    cron_secret = os.getenv("CRON_SECRET")
    fallback = os.getenv("SESLY_CRON_TOKEN")
    auth = request.headers.get("Authorization", "")
    if not ((cron_secret and auth == f"Bearer {cron_secret}") or
            (fallback and request.headers.get("X-Sesly-Cron-Token") == fallback)):
        return jsonify({"error": "unauthorized"}), 401

    rem = _run_reminders()
    ac = _run_auto_complete()
    return jsonify({"ok": True, "reminders": rem, "auto_complete": ac})


@app.route("/cron/admin/preview", methods=["GET"])
def cron_admin_preview():
    """Diagnostic: show current reminder window + bookings inside it,
    WITHOUT sending anything. Public — no secrets returned, just booking
    metadata the owner already sees in the dashboard."""
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    window_start = now_utc + timedelta(minutes=60)
    window_end = now_utc + timedelta(minutes=120)

    out: dict = {
        "now_utc": now_utc.isoformat(),
        "now_baku": (now_utc + timedelta(hours=4)).isoformat(),
        "reminder_window_minutes": "60-120",
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "CRON_SECRET_set": bool(os.getenv("CRON_SECRET")),
        "SESLY_CRON_TOKEN_set": bool(os.getenv("SESLY_CRON_TOKEN")),
    }

    try:
        c = db.client()
        result = (
            c.table("bookings")
            .select(
                "id, bot_id, customer_phone, customer_name, service, "
                "status, scheduled_at, scheduled_time_text, reminder_sent_at, "
                "staff_name_at_booking, "
                "bots(handle, display_name)"
            )
            .eq("status", "confirmed")
            .gte("scheduled_at", window_start.isoformat())
            .lt("scheduled_at", window_end.isoformat())
            .execute()
        )
        rows = result.data or []
        out["confirmed_bookings_in_window"] = len(rows)
        out["already_reminded"] = sum(1 for b in rows if b.get("reminder_sent_at"))
        out["would_send_now"] = sum(1 for b in rows if not b.get("reminder_sent_at"))
        out["bookings"] = [
            {
                "id": b["id"],
                "handle": (b.get("bots") or {}).get("handle"),
                "customer_phone": b.get("customer_phone"),
                "customer_name": b.get("customer_name"),
                "service": b.get("service"),
                "scheduled_at": b.get("scheduled_at"),
                "scheduled_time_text": b.get("scheduled_time_text"),
                "reminder_sent_at": b.get("reminder_sent_at"),
                "minutes_until": int(
                    (datetime.fromisoformat(
                        (b["scheduled_at"] or "").replace("Z", "+00:00")
                    ) - now_utc).total_seconds() / 60
                ) if b.get("scheduled_at") else None,
            }
            for b in rows
        ]
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"

    # Also look at all confirmed bookings in the next 24 hours so the user
    # can see what's queued up even outside the reminder window
    try:
        full = (
            db.client().table("bookings")
            .select(
                "id, customer_phone, customer_name, service, scheduled_at, "
                "scheduled_time_text, status, reminder_sent_at, "
                "bots(handle)"
            )
            .eq("status", "confirmed")
            .gte("scheduled_at", now_utc.isoformat())
            .lt("scheduled_at", (now_utc + timedelta(hours=24)).isoformat())
            .order("scheduled_at")
            .execute()
        )
        out["next_24h_confirmed"] = [
            {
                "handle": (b.get("bots") or {}).get("handle"),
                "scheduled_at": b.get("scheduled_at"),
                "minutes_until": int(
                    (datetime.fromisoformat(
                        (b["scheduled_at"] or "").replace("Z", "+00:00")
                    ) - now_utc).total_seconds() / 60
                ),
                "customer": b.get("customer_name") or b.get("customer_phone"),
                "service": b.get("service"),
                "reminder_sent_at": b.get("reminder_sent_at"),
            }
            for b in (full.data or [])
        ]
    except Exception as e:
        out["next_24h_error"] = f"{e}"

    # Catch-all: show the 20 most recent bookings across ALL statuses /
    # times — so we can see if the test booking exists at all, and if so,
    # in what state.
    try:
        recent = (
            db.client().table("bookings")
            .select(
                "id, customer_phone, customer_name, service, scheduled_at, "
                "scheduled_time_text, status, reminder_sent_at, created_at, "
                "bots(handle)"
            )
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )
        out["last_20_bookings_any_status"] = [
            {
                "id": b.get("id"),
                "handle": (b.get("bots") or {}).get("handle"),
                "status": b.get("status"),
                "scheduled_at": b.get("scheduled_at"),
                "scheduled_time_text": b.get("scheduled_time_text"),
                "minutes_until": (
                    int(
                        (datetime.fromisoformat(
                            (b["scheduled_at"] or "").replace("Z", "+00:00")
                        ) - now_utc).total_seconds() / 60
                    )
                    if b.get("scheduled_at") else None
                ),
                "customer": b.get("customer_name") or b.get("customer_phone"),
                "service": b.get("service"),
                "reminder_sent_at": b.get("reminder_sent_at"),
                "created_at": b.get("created_at"),
            }
            for b in (recent.data or [])
        ]
    except Exception as e:
        out["recent_bookings_error"] = f"{e}"

    return jsonify(out)


@app.route("/cron/admin/trigger", methods=["GET", "POST"])
def cron_admin_trigger():
    """Manually run the reminder + auto-complete logic NOW, bypassing the
    cron schedule. Public — the call is idempotent (reminder_sent_at
    blocks duplicates) so the worst possible "abuse" is making a
    reminder fire a few minutes earlier than scheduled, which is not
    actually a problem worth gating against."""
    rem = _run_reminders()
    ac = _run_auto_complete()
    return jsonify({"ok": True, "reminders": rem, "auto_complete": ac})


def _run_reminders():
    """
    Find confirmed bookings whose scheduled_at is 60-120 min away AND
    we haven't sent a reminder yet — send a reminder via the right
    channel (WhatsApp or Telegram).

    Window is intentionally 60-MIN WIDE (60-120) to be cron-cycle-safe:
    the hourly cron must catch every booking exactly once. With a 30-min
    window (60-90), bookings scheduled in the wrong half of the hour
    would slip through the gap between cron firings. Reminders may go
    out up to 2h early in the worst case, which is acceptable.

    Idempotent via bookings.reminder_sent_at (migration 014).
    """
    try:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        window_start = (now_utc + timedelta(minutes=60)).isoformat()
        window_end = (now_utc + timedelta(minutes=120)).isoformat()

        c = db.client()
        # Find confirmed bookings whose scheduled_at falls in the 60-90 min
        # window from now AND that haven't had a reminder sent
        result = (
            c.table("bookings")
            .select(
                "id, bot_id, customer_phone, customer_name, service, "
                "scheduled_at, scheduled_time_text, reminder_sent_at, "
                "staff_name_at_booking, "
                "bots(display_name, businesses(phone))"
            )
            .eq("status", "confirmed")
            .gte("scheduled_at", window_start)
            .lt("scheduled_at", window_end)
            .is_("reminder_sent_at", "null")
            .execute()
        )
    except Exception as e:
        return {"error": f"db_query: {e}"}

    sent = 0
    skipped = 0
    for b in result.data or []:
        cust_phone = (b.get("customer_phone") or "").strip()
        if not cust_phone:
            skipped += 1
            continue

        # Build the reminder message
        bot_row = b.get("bots") or {}
        biz_name = bot_row.get("display_name") or "Sesly"
        service = b.get("service") or "Randevu"
        when = b.get("scheduled_time_text") or ""
        if not when and b.get("scheduled_at"):
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(b["scheduled_at"].replace("Z", "+00:00"))
                when = dt.strftime("%H:%M")
            except Exception:
                pass
        staff = b.get("staff_name_at_booking") or ""
        staff_part = f" — {staff} ilə" if staff else ""
        cust_name = b.get("customer_name") or ""
        name_part = f"{cust_name}, " if cust_name else ""

        # Pick a time-relative phrase based on how far away the booking is.
        # Window is 60-120 min so "1-2 saata" works as a safe phrasing,
        # but if it's actually ~60 we say "təxminən 1 saata" for warmth.
        minutes_left = None
        try:
            from datetime import datetime as _dt
            dt = _dt.fromisoformat((b.get("scheduled_at") or "").replace("Z", "+00:00"))
            minutes_left = int((dt - now_utc).total_seconds() / 60)
        except Exception:
            pass
        if minutes_left is not None and minutes_left <= 75:
            relative = "Təxminən 1 saata"
        elif minutes_left is not None and minutes_left <= 105:
            relative = "1.5 saata"
        else:
            relative = "Yaxınlarda"

        msg = (
            f"🔔 Salam {name_part}{biz_name}-dan xatırlatma.\n\n"
            f"{relative} — saat {when} — {service}{staff_part} üçün gözləyirik.\n\n"
            f"Gələ bilməsəniz, sadəcə 'ləğv et' yazın."
        )

        # Route by channel — Telegram or WhatsApp. For Telegram, the master
        # token is read from env (TELEGRAM_BOT_TOKEN) by _send_to_customer,
        # so we just call it directly.
        if tg.is_telegram_customer(cust_phone):
            try:
                out = _send_to_customer({}, cust_phone, msg)
                if not out.get("ok"):
                    skipped += 1
                    print(f"[reminder] telegram send failed for {b['id']}: {out.get('error')}")
                    continue
                db.client().table("bookings").update({
                    "reminder_sent_at": datetime.now(timezone.utc).isoformat()
                }).eq("id", b["id"]).execute()
                sent += 1
                print(f"[reminder] sent (tg) for booking={b['id']}")
            except Exception as e:
                print(f"[reminder] tg failed for {b['id']}: {e}")
                skipped += 1
            continue

        # WhatsApp via Meta directly
        digits = "".join(ch for ch in cust_phone if ch.isdigit())
        phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
        if not digits or not phone_number_id:
            skipped += 1
            continue
        try:
            _send_text(phone_number_id, digits, msg)
            # Mark as sent
            db.client().table("bookings").update({
                "reminder_sent_at": datetime.now(timezone.utc).isoformat()
            }).eq("id", b["id"]).execute()
            sent += 1
            print(f"[reminder] sent for booking={b['id']} customer={cust_phone}")
        except Exception as e:
            print(f"[reminder] failed for {b['id']}: {e}")
            skipped += 1

    return {"window_min": "60-120", "sent": sent, "skipped": skipped}


def _run_auto_complete():
    """
    Find confirmed bookings whose scheduled_at + duration finished 1-26
    hours ago AND are still 'confirmed' — auto-mark them as 'completed'
    AND send the review request (same flow as the manual '✓ Bitdi' button).
    """
    try:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        # Look at finished bookings in the last 26 hours (one-day catch-up buffer)
        window_start = (now_utc - timedelta(hours=26)).isoformat()
        window_end = (now_utc - timedelta(hours=1)).isoformat()  # > 1h ago

        c = db.client()
        result = (
            c.table("bookings")
            .select(
                "id, bot_id, customer_phone, customer_name, service, "
                "scheduled_at, duration_minutes, status, review_requested_at, "
                "bots(display_name)"
            )
            .eq("status", "confirmed")
            .gte("scheduled_at", window_start)
            .lt("scheduled_at", window_end)
            .execute()
        )
    except Exception as e:
        return {"error": f"db_query: {e}"}

    completed = 0
    review_sent = 0
    skipped = 0
    for b in result.data or []:
        # Must be past its end (scheduled_at + duration)
        try:
            start = datetime.fromisoformat((b["scheduled_at"] or "").replace("Z", "+00:00"))
            end = start + timedelta(minutes=int(b.get("duration_minutes") or 60))
            if end > now_utc:
                skipped += 1
                continue
        except Exception:
            skipped += 1
            continue

        # Mark completed
        try:
            db.client().table("bookings").update({
                "status": "completed",
                "review_requested_at": now_utc.isoformat(),
                "review_requested_via": "cron",
            }).eq("id", b["id"]).execute()
            completed += 1
        except Exception as e:
            print(f"[auto-complete] update failed for {b['id']}: {e}")
            skipped += 1
            continue

        # Send review request via the right channel
        cust_phone = b.get("customer_phone") or ""
        bot_row = b.get("bots") or {}
        biz_name = bot_row.get("display_name") or "Sesly"
        cust_name = (b.get("customer_name") or "").strip() or "müştəri"
        service = (b.get("service") or "").strip()
        msg = (
            f"Salam {cust_name}! {biz_name}-da görüşünüz necə oldu? 💛\n\n"
            f"1-dən 5-ə qədər ulduz verə bilərsiniz (sadəcə rəqəm yazın). "
            f"Rəyiniz bizə daha yaxşı xidmət vermək üçün kömək edir."
            + (f"\n\n(Xidmət: {service})" if service else "")
        )

        if tg.is_telegram_customer(cust_phone):
            try:
                out = _send_to_customer({}, cust_phone, msg)
                if out.get("ok"):
                    review_sent += 1
                else:
                    print(f"[auto-complete] tg review failed: {out.get('error')}")
            except Exception as e:
                print(f"[auto-complete] tg review send failed: {e}")
            continue

        # WhatsApp path
        digits = "".join(ch for ch in cust_phone if ch.isdigit())
        phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
        if not digits or not phone_number_id:
            continue
        try:
            _send_text(phone_number_id, digits, msg)
            review_sent += 1
        except Exception as e:
            print(f"[auto-complete] review send failed: {e}")

    return {"completed": completed, "review_sent": review_sent, "skipped": skipped}


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

    reply, booking, wants_cancel = ai.generate_reply_with_booking(bot, message, clean_history)
    resp = jsonify({"reply": reply, "booking": booking, "wants_cancel": wants_cancel})
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

    # Reply policy: mirror how the customer messaged.
    #   - Customer typed text     → bot replies with text.
    #   - Customer sent voice AND voice_reply_enabled is ON → bot replies with
    #     voice ONLY (no text duplicate).
    #   - If voice path fails for ANY reason (TTS down, quota, send error),
    #     fall back to text so the customer always gets an answer.
    # Global kill switch — disable voice replies for ALL bots regardless
    # of their voice_reply_enabled toggle. Removed once we ship a higher
    # quality AZ-native voice.
    voice_replies_disabled = os.getenv("VOICE_REPLIES_DISABLED", "").lower() in ("1", "true", "yes")

    should_voice = (
        not voice_replies_disabled
        and message_type == "voice"
        and served_by
        and served_by.get("voice_reply_enabled")
    )

    voice_sent = False
    if should_voice:
        try:
            import tts
            if not tts.is_configured():
                print(
                    "[voice-reply] falling back to text: ELEVENLABS_API_KEY "
                    "not set on this deployment."
                )
            else:
                voice_id = served_by.get("voice_voice_id")
                print(f"[voice-reply] synthesizing with voice_id={voice_id or '<default>'}")
                audio_path = tts.synthesize(reply, voice_id)
                if audio_path:
                    send_result = _send_audio(phone_number_id, reply_to, audio_path)
                    if send_result.get("ok"):
                        voice_sent = True
                        print(
                            f"[voice-reply] voice sent OK "
                            f"({send_result.get('kind')}). Skipping text."
                        )
                    else:
                        print(
                            f"[voice-reply] WhatsApp audio send failed: "
                            f"{send_result.get('error')}. Falling back to text."
                        )
                else:
                    print("[voice-reply] tts.synthesize returned None — falling back to text.")
        except Exception as e:
            print(f"[voice-reply] best-effort failed: {e} — falling back to text.")
    else:
        # Log why voice wasn't even attempted (only when the customer spoke)
        if message_type == "voice":
            if not served_by:
                print("[voice-reply] not attempted: no served_by bot (menu / no-bot path)")
            elif not served_by.get("voice_reply_enabled"):
                print(
                    f"[voice-reply] not attempted: voice_reply_enabled is OFF "
                    f"for bot {served_by.get('handle')!r}."
                )

    if not voice_sent:
        _send_text(phone_number_id, reply_to, reply)


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5001"))
    debug = os.getenv("FLASK_ENV") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug)
