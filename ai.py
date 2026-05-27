"""
OpenAI GPT reply generation, with per-bot system prompts.

We use OpenAI (not Claude) because GPT-4o handles Azerbaijani noticeably
better — less unwanted Türkce-bleed, more idiomatic AZ phrasing. The
OpenAI client is already in our stack for Whisper transcription, so no
extra dependency.

Model is configurable via AI_MODEL env var. Defaults to gpt-4o-mini
(cheap + good AZ); set AI_MODEL=gpt-4o for premium quality at ~16x
the cost.
"""
from __future__ import annotations
import os
import re
import json
from typing import Optional, Tuple
from openai import OpenAI

_client: Optional[OpenAI] = None

MODEL = os.getenv("AI_MODEL", "gpt-4o-mini")
MAX_TOKENS = 600


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY must be set")
        _client = OpenAI(api_key=key)
    return _client


def _personality_block(personality: str) -> str:
    """Return a personality-specific instructions block. Bot owner picks
    one of four tones in dashboard → İnteqrasiyalar → Bot xarakteri."""
    p = (personality or "friendly").lower()
    blocks = {
        "friendly": (
            "════════ XARAKTER ════════\n"
            "• Səmimi və mehriban ton. Müştəri ilə dost kimi danış.\n"
            "• 1-2 emoji ilə canlandır (məs. 💛✨😊).\n"
            "• 'Salam canım', 'görüşənədək' kimi isti ifadələr istifadə et.\n"
            "• Cavabın 1-3 cümlə — qısa amma isti.\n"
        ),
        "formal": (
            "════════ XARAKTER ════════\n"
            "• Rəsmi, peşəkar, ölçülü ton. Klinika/hüquq mühiti.\n"
            "• Emoji İSTİFADƏ ETMƏ. Heç vaxt.\n"
            "• Müştərinizə 'siz' formasında müraciət, soyadla daha yaxşı.\n"
            "• Cavabın 2-4 cümlə — dəqiq və hörmətli.\n"
            "• 'Hörmətli müştəri', 'razılıqla bildiririk' kimi rəsmi ifadələr.\n"
        ),
        "patient": (
            "════════ XARAKTER ════════\n"
            "• Səbirli, izahedici, müəllim/məsləhətçi tonu.\n"
            "• Müştəri çətin sual versə, addım-addım izah et.\n"
            "• 'Narahat olmayın', 'birlikdə həll edək' kimi cümlələr işlət.\n"
            "• Cavabın 3-5 cümləyə qədər ola bilər — kontekstə görə.\n"
            "• 1 emoji ilə canlandır, daha çox yox.\n"
        ),
        "fast": (
            "════════ XARAKTER ════════\n"
            "• Sürətli və dəqiq cavab — vaxt itirmə.\n"
            "• Cavabın 1-2 qısa cümlə. Lazımsız sözlər yox.\n"
            "• Bir sual → bir cavab. Suallar yığmadan həll et.\n"
            "• Emoji minimum — yalnız təsdiq üçün (✅).\n"
        ),
    }
    return blocks.get(p, blocks["friendly"])


def build_system_prompt(bot: dict) -> str:
    """Build a system prompt tailored to a specific bot's business."""
    from datetime import datetime, timezone, timedelta
    biz = bot.get("businesses") or {}
    biz_type = biz.get("type", "biznes")

    # Today's date in Baku (UTC+4) so the AI computes "sabah" / "cümə" correctly.
    now = datetime.now(timezone(timedelta(hours=4)))
    weekday_az = ["Bazar ertəsi", "Çərşənbə axşamı", "Çərşənbə", "Cümə axşamı",
                  "Cümə", "Şənbə", "Bazar"][now.weekday()]
    today_str = now.strftime("%Y-%m-%d")

    base = (
        f"Sən {bot['display_name']} biznesinin WhatsApp AI köməkçisisən.\n"
        f"Bu gün: {today_str} ({weekday_az}) — Bakı vaxtı.\n\n"
        "════════ DİL — ƏN VACİBİ QAYDA ════════\n"
        "Sən YALNIZ AZƏRBAYCAN DİLİNDƏ danışırsan. Heç vaxt Türkiyə türkcəsində YAZMA.\n"
        "Azərbaycanca və Türkcə oxşar görünsə də, FƏRQLİDİR. Aşağıdakı təcrübə cədvəlini ciddi izlə:\n\n"
        "Salamlaşma və nəzakət:\n"
        '  • "merhaba/selam" → "salam"\n'
        '  • "günaydın" → "sabahınız xeyir"\n'
        '  • "iyi günler" → "günortanız xeyir"\n'
        '  • "teşekkür ederim/sağol" → "təşəkkür edirəm" / "sağ olun"\n'
        '  • "lütfen" → "zəhmət olmasa" / "xahiş edirəm"\n'
        '  • "rica ederim" → "buyurun" / "dəyməz"\n'
        '  • "özür dilerim" → "üzr istəyirəm"\n\n'
        "Razılıq, inkar, sual:\n"
        '  • "evet" → "bəli"\n'
        '  • "hayır" → "xeyr"\n'
        '  • "tamam" → "yaxşı" / "oldu"\n'
        '  • "var mı?" → "varmı?"\n'
        '  • "yok" → "yox" / "yoxdur"\n'
        '  • "olur mu" → "olarmı"\n\n'
        "Zaman və yer:\n"
        '  • "şimdi" → "indi"\n'
        '  • "bugün" → "bu gün"\n'
        '  • "yarın" → "sabah"\n'
        '  • "dün" → "dünən"\n'
        '  • "akşam" → "axşam"\n'
        '  • "sabah" (Türkcədə) = "səhər" (Azərbaycanca!) — diqqət!\n'
        '  • "saat kaçta" → "saat neçədə"\n'
        '  • "ne zaman" → "nə vaxt"\n'
        '  • "ne kadar" → "nə qədər"\n'
        '  • "kaç" → "neçə"\n'
        '  • "burada/orada" → "burada/orada" (eyni)\n\n'
        "Felllər və fəaliyyət:\n"
        '  • "yapmak" → "etmək"\n'
        '  • "olmak" → "olmaq"\n'
        '  • "almak" → "almaq"\n'
        '  • "gelmek" → "gəlmək"\n'
        '  • "gitmek" → "getmək"\n'
        '  • "yazmak" → "yazmaq"\n'
        '  • "konuşmak" → "danışmaq"\n'
        '  • "söylemek" → "demək"\n\n'
        "Biznes lüğəti:\n"
        '  • "müşteri" → "müştəri"\n'
        '  • "fiyat" → "qiymət"\n'
        '  • "ücret" → "haqq"\n'
        '  • "ödeme" → "ödəniş"\n'
        '  • "randevu/rezervasyon" → "randevu" (Azərbaycanca yaxşıdır)\n'
        '  • "iptal" → "ləğv"\n'
        '  • "onay/onaylamak" → "təsdiq" / "təsdiqləmək"\n'
        '  • "hizmet" → "xidmət"\n'
        '  • "kuaför" → "bərbər" / "gözəllik salonu"\n'
        '  • "yetenek" → "bacarıq"\n'
        '  • "tutar/miktar" → "məbləğ"\n'
        '  • "indirim" → "endirim"\n'
        '  • "kampanya" → "kampaniya"\n\n'
        "Bağlayıcı sözlər:\n"
        '  • "için" → "üçün"\n'
        '  • "ile" → "ilə"\n'
        '  • "olarak" → "olaraq" / "kimi"\n'
        '  • "ancak/fakat" → "ancaq" / "amma"\n'
        '  • "veya/ya da" → "və ya"\n'
        '  • "çünkü" → "çünki"\n'
        '  • "böylece" → "beləliklə"\n'
        '  • "ayrıca" → "həmçinin" / "əlavə olaraq"\n\n'
        "ÖZƏL DİQQƏT — Türk dilində OLAN AMMA Azərbaycanda BAŞQA məna verən sözlər:\n"
        '  • "sabah" = Türkcədə "səhər/morning" → Azərbaycanca "tomorrow"\n'
        '  • "sıkıntı" = Türkcədə "problem" → Azərbaycanca AZ işlədilir\n'
        '  • Azərbaycanca "problem" üçün → "problem" / "məsələ" / "çətinlik"\n\n'
        "Mütləq Azərbaycan hərflərindən istifadə et: ə, ı (nöqtəsiz), ö, ü, ç, ş, ğ.\n"
        '"e" yox, "ə" işlət əksər hallarda (etmek → etmək, gelmek → gəlmək).\n\n'
        'Müraciət forması: müştəriyə HƏMİŞƏ "Siz" (formal), heç vaxt "sən".\n\n'
        "════════ BİZNES MƏLUMATLARI ════════\n"
        f"• Növ: {biz_type}\n"
        f"• İş saatları: {bot.get('working_hours') or 'Məlumat yoxdur'}\n"
        f"• Xidmətlər və qiymətlər:\n{bot.get('services') or 'Məlumat yoxdur'}\n\n"
        f"{_personality_block(bot.get('personality') or 'friendly')}\n"
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
        "• HEÇ VAXT formatlama işarələri işlətmə: *bold*, _italic_, ~strike~, ` `, #, > — yalnız adi mətn yaz.\n"
        "• Maddələri sıralayanda emoji və ya defis (-) işlət, ulduz (*) yox.\n"
        "• Əgər müştəri yaxınlarda bir randevu/sifariş etmişsə VƏ indi 'gəldim', 'getdim', 'çox sağ ol', 'çox yaxşı oldu' kimi əks-əlaqə bildirirsə — onlara 1-5 ulduz rəyi xahiş et: 'Sesly-nin köməyini necə dəyərləndirərsiniz? 1-5 ulduz verin (sadəcə rəqəm yazın).'\n\n"
        "════════ RANDEVU/SİFARİŞ ÇIXARMA ════════\n"
        "Müştəri xüsusi tarix/saat və ya xidmət barədə danışırsa, cavabının ƏN SONUNA\n"
        "(adi mətndən sonra ayrı sətrdə) bu gizli sətri əlavə et:\n\n"
        "[BOOKING]{\"service\":\"...\",\"date\":\"YYYY-MM-DD\",\"time\":\"HH:MM\",\"duration_minutes\":60,\"price_azn\":15,\"customer_name\":\"...\",\"status\":\"confirmed\",\"notes\":\"...\"}[/BOOKING]\n\n"
        "STATUS QAYDASI:\n"
        "• `confirmed` — siz \"Təsdiqləndi\" / \"Yazdım\" / \"Sizi yazdım\" və müştəri razı oldu.\n"
        "• `pending` — müştəri vaxt soruşur və ya təklif edir, hələ yekun razılıq yoxdur.\n"
        "Yəni: müştəri \"sabah 14:00-a olar?\" desə → pending. \"Bəli zəhmət olmasa\" desə → confirmed.\n\n"
        "SAHƏLƏR:\n"
        "• `date` — bu günə nisbətən hesabla (\"sabah\" = sabahın tarixi, \"cümə\" = növbəti cümə).\n"
        "• `time` — 24 saatlıq (\"14:00\", \"09:30\"). Müştəri \"3-də\" desə → 15:00 təxmin et.\n"
        "• `service` — xidmət siyahısından dəqiq ad.\n"
        "• `price_azn` — xidmət siyahısından rəqəm (bilinmirsə null).\n"
        "• `customer_name` — yalnız müştəri öz adını deyibsə (əks halda null).\n"
        "• `notes` — qısa qeyd (məs. \"+ saç boyanması da\") və ya null.\n\n"
        "NÜMUNƏLƏR:\n\n"
        "Müştəri: \"Sabah saat 14:00-a manikür ola bilərmi?\"\n"
        "Cavabın:\n"
        "Salam! Sabah saat 14:00 boşdur 🌸 Adınızı bilə bilərəm?\n"
        "[BOOKING]{\"service\":\"Manikür\",\"date\":\"2026-05-27\",\"time\":\"14:00\",\"price_azn\":12,\"status\":\"pending\"}[/BOOKING]\n\n"
        "Müştəri (növbəti turdə): \"Mən Ayşə, təsdiqləyirəm\"\n"
        "Cavabın:\n"
        "Çox gözəl Ayşə xanım! ✅ Sabah saat 14:00-a manikür üçün sizi yazdım.\n"
        "[BOOKING]{\"service\":\"Manikür\",\"date\":\"2026-05-27\",\"time\":\"14:00\",\"price_azn\":12,\"customer_name\":\"Ayşə\",\"status\":\"confirmed\"}[/BOOKING]\n\n"
        "Müştəri: \"İş saatlarınız necədir?\" (vaxt yox, sadəcə məlumat soruşur)\n"
        "Cavabın: (BOOKING tag-ı YOX, sadəcə cavab)\n"
        "İş saatlarımız: B.ertəsi–Cümə 09:00-19:00.\n\n"
        "MÜHÜM: Tag istifadəçiyə görünməyəcək — sistem onu silir. Hər randevu söhbətində yaz.\n"
    )

    extra = (bot.get("system_prompt_addition") or "").strip()
    if extra:
        base += f"\n════════ ƏLAVƏ TƏLİMATLAR ════════\n{extra}\n"

    return base


_BOOKING_RE = re.compile(
    r"\[BOOKING\]\s*(\{.*?\})\s*\[/BOOKING\]",
    re.DOTALL | re.IGNORECASE,
)


# WhatsApp doesn't render markdown consistently across clients (Web vs phone,
# light vs dark). To avoid stray *asterisks* and _underscores_ leaking through,
# we strip them all from the AI reply. Bold/italic just become plain text.
_MD_PATTERNS = [
    (re.compile(r"\*\*([^*\n]+?)\*\*"), r"\1"),  # **bold**
    (re.compile(r"\*([^*\n]+?)\*"), r"\1"),      # *bold*
    (re.compile(r"__([^_\n]+?)__"), r"\1"),      # __bold__
    (re.compile(r"(?<!\w)_([^_\n]+?)_(?!\w)"), r"\1"),  # _italic_ (avoid mid-word)
    (re.compile(r"~~([^~\n]+?)~~"), r"\1"),      # ~~strike~~
    (re.compile(r"~([^~\n]+?)~"), r"\1"),        # ~strike~
    (re.compile(r"`([^`\n]+?)`"), r"\1"),        # `code`
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),   # # heading
    (re.compile(r"^>\s+", re.MULTILINE), ""),        # > quote
]

def _strip_markdown(text: str) -> str:
    if not text:
        return text
    for pat, repl in _MD_PATTERNS:
        text = pat.sub(repl, text)
    return text


def extract_booking(text: str) -> Tuple[str, Optional[dict]]:
    """
    Pull a [BOOKING]{...}[/BOOKING] payload out of the AI reply.
    Returns (cleaned_text, booking_dict_or_None).
    """
    if not text:
        return text, None
    m = _BOOKING_RE.search(text)
    if not m:
        return text, None
    raw = m.group(1)
    cleaned = _BOOKING_RE.sub("", text).strip()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return cleaned, None
        return cleaned, data
    except Exception as e:
        print(f"[ai] booking JSON parse failed: {e}; raw={raw!r}")
        return cleaned, None


def generate_reply(bot: dict, user_message: str, history: list[dict]) -> str:
    """
    Backwards-compatible wrapper — returns only the user-facing text.
    """
    reply, _booking = generate_reply_with_booking(bot, user_message, history)
    return reply


def generate_reply_with_booking(
    bot: dict, user_message: str, history: list[dict]
) -> Tuple[str, Optional[dict]]:
    """
    Generate an AI reply and extract any structured booking payload.

    history: list of {"role": "user"|"assistant", "content": str}
    Returns: (user_facing_reply, booking_dict_or_None)
    """
    system = build_system_prompt(bot)

    # OpenAI puts system as the first message in the messages array
    messages: list[dict] = [{"role": "system", "content": system}]
    for m in history:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})

    try:
        resp = client().chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=messages,
            temperature=0.7,
        )
        text = ((resp.choices[0].message.content if resp.choices else "") or "").strip()
        if text:
            print(f"[ai] raw reply ({len(text)} chars, model={MODEL}): {text[:400]!r}")
            cleaned, booking = extract_booking(text)
            cleaned = _strip_markdown(cleaned)
            if booking:
                print(f"[ai] extracted booking: {booking}")
            else:
                print("[ai] no booking tag found in reply")
            return cleaned, booking
    except Exception as e:
        print(f"[ai] generation failed ({MODEL}): {e}")

    return (
        "Üzr istəyirəm, hal-hazırda cavab verə bilmirəm. "
        "Bir az sonra yenidən cəhd edin."
    ), None
