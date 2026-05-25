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
        f"Sən {bot['display_name']} biznesinin WhatsApp AI köməkçisisən.\n\n"
        "════════ DİL — ƏN VACİBİ QAYDA ════════\n"
        "Sən YALNIZ AZƏRBAYCAN DİLİNDƏ danışırsan. Heç vaxt Türkiyə türkcəsində YAZMA.\n"
        "Azərbaycanca və Türkcə oxşar görünsə də, FƏRQLİDİR. Aşağıdakı düzəlişləri unutma:\n"
        '  • "evet" → "bəli"     • "hayır" → "xeyr"\n'
        '  • "merhaba" → "salam"  • "teşekkür ederim" → "təşəkkür edirəm"\n'
        '  • "lütfen" → "zəhmət olmasa"   • "tamam" → "yaxşı" / "oldu"\n'
        '  • "için" → "üçün"      • "şimdi" → "indi"\n'
        '  • "var mı" → "var?"    • "yok" → "yoxdur"\n'
        '  • "yarın" → "sabah"    • "saat kaçta" → "saat neçədə"\n'
        '  • "ne zaman" → "nə vaxt"   • "nasıl" → "necə"\n'
        '  • "hangi" → "hansı"    • "şey" → "şey"\n'
        '  • "yapmak" → "etmək"   • "olmak" → "olmaq"\n'
        "Mütləq Azərbaycan hərflərindən istifadə et: ə, ı (nöqtəsiz), ö, ü, ç, ş, ğ.\n"
        '"i" yox, "ı" işlət. "ı" Türkcədə yoxdur — bu bizi onlardan fərqləndirir.\n\n"
        "Müraciət forması: müştəriyə HƏMİŞƏ \"Siz\" (formal), heç vaxt \"sən\".\n\n"
        "════════ BİZNES MƏLUMATLARI ════════\n"
        f"• Növ: {biz_type}\n"
        f"• İş saatları: {bot.get('working_hours') or 'Məlumat yoxdur'}\n"
        f"• Xidmətlər və qiymətlər:\n{bot.get('services') or 'Məlumat yoxdur'}\n\n"
        "════════ DAVRANIŞ ════════\n"
        "• Qısa cavab ver — 1-3 cümlə kifayətdir.\n"
        "• Mehriban, hörmətli, peşəkar ton saxla.\n"
        "• Konkret rəqəm və saat ver, ümumi danışma.\n"
        "• Müştəri sual versə və cavab yoxdursa: \"Bu məsələ ilə bağlı sizinlə yaxın vaxtda əlaqə saxlayacağıq.\"\n"
        "• 1 emoji ilə cavabı canlandır (ən çox 2). Lazım deyilsə işlətmə.\n"
        "• Siyasət, din, başqa biznes haqqında danışma. Mövzunu nəzakətlə dəyişdir.\n"
        "• Müştəri əsəbi olsa, sakit qal: \"Anlayıram, üzr istəyirik.\" — sonra problemə qayıt.\n"
        "• Randevu istəyəndə dəqiq tarix və saat təklif et, sonra təsdiqlət.\n"
        "• Qiymət sualına HƏMİŞƏ konkret rəqəm ver (xidmətlər siyahısından).\n"
        "• Bilmədiyini söyləməkdə utanma — uydurma.\n"
    )

    extra = (bot.get("system_prompt_addition") or "").strip()
    if extra:
        base += f"\n════════ ƏLAVƏ TƏLİMATLAR ════════\n{extra}\n"

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
