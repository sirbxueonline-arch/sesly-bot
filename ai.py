"""
Claude (Anthropic) reply generation, with per-bot system prompts.
"""
from __future__ import annotations
import os
from typing import Optional
from anthropic import Anthropic

_client: Optional[Anthropic] = None

MODEL = "claude-haiku-4-5"
MAX_TOKENS = 350


def client() -> Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY must be set")
        _client = Anthropic(api_key=key)
    return _client


def build_system_prompt(bot: dict) -> str:
    """Build a system prompt tailored to a specific bot's business."""
    biz = bot.get("businesses") or {}
    biz_type = biz.get("type", "biznes")

    base = (
        f"Sən {bot['display_name']}-in WhatsApp köməkçisisən.\n"
        f"Biznes növü: {biz_type}\n"
        f"İş saatları: {bot.get('working_hours') or 'Məlumat yoxdur'}\n"
        f"Xidmətlər: {bot.get('services') or 'Məlumat yoxdur'}\n\n"
        "Qaydalar:\n"
        "- HƏMİŞƏ Azərbaycan dilində cavab ver.\n"
        "- Qısa ol — maksimum 3-4 cümlə.\n"
        "- Mehriban və peşəkar ton saxla.\n"
        "- Randevu, qiymət, iş saatları haqqında dəqiq məlumat ver.\n"
        "- Bilmədiyini etiraf et: \"Bu məsələ ilə əlaqədar sizinlə əlaqə saxlayacağıq\".\n"
        "- Emoji işlət, amma az (1-2 ədəd)."
    )

    extra = (bot.get("system_prompt_addition") or "").strip()
    if extra:
        base += f"\n\nƏlavə təlimatlar:\n{extra}"

    return base


def generate_reply(bot: dict, user_message: str, history: list[dict]) -> str:
    """
    Generate an AI reply for the given bot using prior conversation history.
    history: list of {"role": "user"|"assistant", "content": str}
    """
    system = build_system_prompt(bot)

    # Build messages: history + current user message
    messages: list[dict] = []
    for m in history:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    try:
        resp = client().messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
        )
        # Concatenate text blocks
        chunks = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                chunks.append(block.text)
        text = "".join(chunks).strip()
        if text:
            return text
    except Exception as e:
        print(f"[ai] generation failed: {e}")

    return (
        "Üzr istəyirəm, hal-hazırda cavab verə bilmirəm. "
        "Bir az sonra yenidən cəhd edin."
    )
