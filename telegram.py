"""
Sesly — Telegram bot helpers.

Each Sesly bot can OPTIONALLY have its own Telegram bot. The owner creates a
bot with @BotFather, gets a token, pastes it into the dashboard, and Sesly
registers a webhook pointing back to /telegram/webhook/<bot_id>.

Customer identifier convention:
    WhatsApp:  "+994501234567"
    Telegram:  "tg:<chat_id>"     e.g. "tg:123456789"

This module only talks to the Telegram Bot API. The actual message routing
(menus, handles, bookings, history, AI) lives in app.py and reuses the same
_handle_message() that WhatsApp uses.
"""
from __future__ import annotations
import os
import tempfile
from typing import Optional

import requests

TG_API = "https://api.telegram.org"


def _api(token: str, method: str) -> str:
    return f"{TG_API}/bot{token}/{method}"


def _file_api(token: str, file_path: str) -> str:
    return f"{TG_API}/file/bot{token}/{file_path}"


# ---------------------------------------------------------------------------
# Setup / validation
# ---------------------------------------------------------------------------

def get_me(token: str) -> dict:
    """Validate a token and return bot info.
    Response shape:
      { ok: bool, username?: str, id?: int, first_name?: str, error?: str }
    """
    try:
        r = requests.get(_api(token, "getMe"), timeout=10)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:200]}"}
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description") or "telegram_error"}
        result = data.get("result") or {}
        return {
            "ok": True,
            "id": result.get("id"),
            "username": result.get("username"),
            "first_name": result.get("first_name"),
            "can_join_groups": result.get("can_join_groups"),
        }
    except Exception as e:
        return {"ok": False, "error": f"exception: {e}"}


def set_webhook(token: str, webhook_url: str, secret: Optional[str] = None) -> dict:
    """Register the webhook with Telegram. secret_token is sent back in
    X-Telegram-Bot-Api-Secret-Token header on every update."""
    try:
        payload = {
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
        }
        if secret:
            payload["secret_token"] = secret
        r = requests.post(_api(token, "setWebhook"), json=payload, timeout=15)
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description") or "set_webhook_failed"}
        return {"ok": True, "description": data.get("description")}
    except Exception as e:
        return {"ok": False, "error": f"exception: {e}"}


def delete_webhook(token: str) -> dict:
    """Remove the webhook (used on disconnect)."""
    try:
        r = requests.post(
            _api(token, "deleteWebhook"),
            json={"drop_pending_updates": True},
            timeout=10,
        )
        data = r.json()
        return {"ok": bool(data.get("ok")), "description": data.get("description")}
    except Exception as e:
        return {"ok": False, "error": f"exception: {e}"}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def send_message(token: str, chat_id: str | int, text: str) -> dict:
    """Send a text message. Chunks if > 4096 chars (Telegram limit)."""
    if not token or not chat_id:
        return {"ok": False, "error": "missing token or chat_id"}
    # Telegram supports up to 4096 chars per message
    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
    last = {"ok": True}
    for chunk in chunks:
        try:
            r = requests.post(
                _api(token, "sendMessage"),
                json={
                    "chat_id": chat_id,
                    "text": chunk,
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            data = r.json()
            if not data.get("ok"):
                return {"ok": False, "error": data.get("description") or "send_failed"}
            last = {"ok": True, "message_id": (data.get("result") or {}).get("message_id")}
        except Exception as e:
            return {"ok": False, "error": f"exception: {e}"}
    return last


def send_voice(token: str, chat_id: str | int, local_path: str) -> dict:
    """Send an audio file as a Telegram voice note. OGG/Opus only renders as
    a true voice note; MP3 renders as audio attachment."""
    if not token or not chat_id:
        return {"ok": False, "error": "missing token or chat_id"}
    ext = os.path.splitext(local_path)[1].lower()
    method = "sendVoice" if ext == ".ogg" else "sendAudio"
    field = "voice" if ext == ".ogg" else "audio"
    try:
        with open(local_path, "rb") as f:
            r = requests.post(
                _api(token, method),
                data={"chat_id": chat_id},
                files={field: f},
                timeout=30,
            )
        data = r.json()
        if not data.get("ok"):
            return {"ok": False, "error": data.get("description") or "voice_send_failed"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"exception: {e}"}
    finally:
        try:
            os.unlink(local_path)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Voice download (for transcription via Whisper)
# ---------------------------------------------------------------------------

def download_voice_file(token: str, file_id: str) -> Optional[bytes]:
    """Two-step Telegram voice download:
      1) getFile → file_path
      2) GET https://api.telegram.org/file/bot<token>/<file_path>
    Returns raw bytes or None on failure."""
    try:
        meta = requests.get(
            _api(token, "getFile"),
            params={"file_id": file_id},
            timeout=10,
        )
        if meta.status_code != 200:
            print(f"[tg] getFile {meta.status_code}: {meta.text[:200]}")
            return None
        meta_data = meta.json()
        if not meta_data.get("ok"):
            print(f"[tg] getFile not ok: {meta_data}")
            return None
        file_path = (meta_data.get("result") or {}).get("file_path")
        if not file_path:
            return None
        # Download the actual file
        resp = requests.get(_file_api(token, file_path), timeout=30)
        if resp.status_code != 200:
            print(f"[tg] file download {resp.status_code}")
            return None
        return resp.content
    except Exception as e:
        print(f"[tg] download_voice_file exception: {e}")
        return None


def transcribe_voice(token: str, file_id: str, language: str = "az") -> Optional[str]:
    """Download a Telegram voice file and transcribe via Whisper.
    voice.py's transcribe() takes a file path and deletes the file when done,
    so we just write the bytes to a temp .ogg and hand it off."""
    audio_bytes = download_voice_file(token, file_id)
    if not audio_bytes:
        return None
    try:
        import voice as voice_module
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        # voice_module.transcribe takes care of cleanup
        return voice_module.transcribe(tmp_path, language=language)
    except Exception as e:
        print(f"[tg] transcribe exception: {e}")
        return None


# ---------------------------------------------------------------------------
# Identifier helpers
# ---------------------------------------------------------------------------

def chat_id_to_customer_phone(chat_id: int | str) -> str:
    """Convert a Telegram chat_id to our customer_phone format."""
    return f"tg:{chat_id}"


def customer_phone_to_chat_id(customer_phone: str) -> Optional[str]:
    """If this customer_phone is a Telegram chat_id, return the bare id.
    Otherwise return None (it's a WhatsApp phone)."""
    if not customer_phone:
        return None
    if customer_phone.startswith("tg:"):
        return customer_phone[3:]
    return None


def is_telegram_customer(customer_phone: str) -> bool:
    return bool(customer_phone and customer_phone.startswith("tg:"))
