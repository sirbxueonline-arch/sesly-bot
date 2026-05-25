"""
Routes incoming WhatsApp messages to the correct bot.

Handle detection rules:
- Message starts with "/" → extract handle, look up bot, set session.
- Message is "/menu" → show onboarding message.
- No "/" → use customer's current active bot session.
- No active session → send onboarding message.
"""
from __future__ import annotations
from typing import Optional


MENU_MESSAGE = (
    "Salam! Sesly-ə xoş gəldiniz 👋\n\n"
    "Bir biznesə qoşulmaq üçün onun kodunu yazın:\n"
    "Məsələn: /alcipan\n\n"
    "Əgər kodunu bilmirsinizsə, müvafiq biznesin Sesly kodunu onlardan soruşun."
)

NO_BOT_MESSAGE = (
    "Hər hansı bir biznesə qoşulmaq üçün onların Sesly kodunu yazın.\n"
    "Məsələn: /alcipan\n\n"
    "Kod bilmirsinizsə, müvafiq biznesdən soruşun."
)

HANDLE_NOT_FOUND = "Bu kod tapılmadı. Düzgün kodu yazın və ya /menu yazaraq başlanğıca qayıdın."


def parse_handle(message: str) -> Optional[str]:
    """If message starts with `/`, return the handle (no slash). Else None."""
    if not message:
        return None
    msg = message.strip()
    if msg.startswith("/"):
        first = msg.split()[0]
        return first[1:].lower() or None
    return None


def get_remaining_message(message: str) -> str:
    """
    If the message contains a handle PLUS extra text (e.g. '/alcipan salam'),
    return the extra text. Otherwise return the full message.
    """
    if not message:
        return ""
    parts = message.strip().split(maxsplit=1)
    if len(parts) > 1 and parts[0].startswith("/"):
        return parts[1].strip()
    return message.strip()


def is_menu_command(handle: Optional[str]) -> bool:
    return handle == "menu"
