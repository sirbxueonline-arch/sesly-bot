"""
Supabase client for the Flask bot.
Uses SERVICE ROLE key — bypasses RLS so the bot can read any bot config
and write messages on behalf of any business.
"""
from __future__ import annotations
import os
from typing import Optional
from supabase import create_client, Client

_supabase: Optional[Client] = None


def client() -> Client:
    global _supabase
    if _supabase is None:
        url = os.getenv("SUPABASE_URL")
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set"
            )
        _supabase = create_client(url, key)
    return _supabase


# ---------------- Bot lookup ----------------

def get_bot_by_handle(handle: str) -> Optional[dict]:
    """Look up bot config by handle (e.g. 'alcipan')."""
    h = (handle or "").lower().strip()
    # Step 1: try the canonical lookup (handle + active)
    try:
        result = (
            client()
            .table("bots")
            .select("*, businesses(name, type, plan)")
            .eq("handle", h)
            .eq("is_active", True)
            .maybe_single()
            .execute()
        )
        if result and result.data:
            return result.data
    except Exception as e:
        print(f"[db] get_bot_by_handle (active+join) failed: {e}")

    # Step 2: fallback — just by handle, no join, no is_active filter
    try:
        result = (
            client()
            .table("bots")
            .select("*")
            .eq("handle", h)
            .maybe_single()
            .execute()
        )
        if result and result.data:
            print(
                f"[db] bot {h!r} exists but failed primary lookup; "
                f"is_active={result.data.get('is_active')}"
            )
            if result.data.get("is_active"):
                return result.data
    except Exception as e:
        print(f"[db] get_bot_by_handle (fallback) failed: {e}")

    print(f"[db] no active bot found for handle={h!r}")
    return None


# Plan limits — mirrors lib/plans.ts in the dashboard
PLAN_LIMITS = {
    "free":  {"messages": 100,  "bots": 1, "name": "Sınaq"},
    "start": {"messages": 1000, "bots": 3, "name": "Başlanğıc"},
    "pro":   {"messages": None, "bots": 5, "name": "Pro"},
}


def get_monthly_user_message_count(business_id: str) -> int:
    """Count customer (user-role) messages in the current calendar month."""
    if not business_id:
        return 0
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

        bots = (
            client()
            .table("bots")
            .select("id")
            .eq("business_id", business_id)
            .execute()
        )
        bot_ids = [b["id"] for b in (bots.data or [])]
        if not bot_ids:
            return 0

        convs = (
            client()
            .table("conversations")
            .select("id")
            .in_("bot_id", bot_ids)
            .execute()
        )
        conv_ids = [c["id"] for c in (convs.data or [])]
        if not conv_ids:
            return 0

        result = (
            client()
            .table("messages")
            .select("id", count="exact")
            .in_("conversation_id", conv_ids)
            .eq("role", "user")
            .gte("created_at", month_start)
            .execute()
        )
        return result.count or 0
    except Exception as e:
        print(f"[db] message count failed: {e}")
        return 0


def is_over_message_limit(bot: dict) -> bool:
    """Returns True if the bot's business has hit its monthly message cap."""
    biz = bot.get("businesses") or {}
    plan = (biz.get("plan") or "free").lower()
    cap = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["messages"]
    if cap is None:
        return False
    used = get_monthly_user_message_count(bot.get("business_id"))
    print(f"[plan] {plan}: {used}/{cap} messages this month")
    return used >= cap


def get_plan_name(bot: dict) -> str:
    biz = bot.get("businesses") or {}
    plan = (biz.get("plan") or "free").lower()
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["name"]


def get_active_bot(customer_phone: str) -> Optional[dict]:
    """Get which bot this customer is currently talking to."""
    try:
        result = (
            client()
            .table("customer_sessions")
            .select("bot_id, bots(*, businesses(name, type))")
            .eq("customer_phone", customer_phone)
            .single()
            .execute()
        )
        if result.data and result.data.get("bots"):
            return result.data["bots"]
    except Exception:
        pass
    return None


def set_active_bot(customer_phone: str, bot_id: str) -> None:
    """Set or update which bot this customer is talking to."""
    client().table("customer_sessions").upsert(
        {
            "customer_phone": customer_phone,
            "bot_id": bot_id,
            "last_active_at": "now()",
        },
        on_conflict="customer_phone",
    ).execute()


def clear_active_bot(customer_phone: str) -> None:
    """Disconnect the customer from any active bot session."""
    try:
        client().table("customer_sessions").delete().eq(
            "customer_phone", customer_phone
        ).execute()
    except Exception:
        pass


# ---------------- Messages / conversations ----------------

def _get_or_create_conversation(bot_id: str, customer_phone: str) -> Optional[str]:
    """Return the conversation id for (bot, phone), creating if needed."""
    try:
        existing = (
            client()
            .table("conversations")
            .select("id")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .maybe_single()
            .execute()
        )
        if existing and existing.data:
            return existing.data["id"]
    except Exception:
        pass

    created = (
        client()
        .table("conversations")
        .insert(
            {
                "bot_id": bot_id,
                "customer_phone": customer_phone,
            }
        )
        .execute()
    )
    if created.data:
        return created.data[0]["id"]
    return None


def save_message(
    bot_id: str,
    customer_phone: str,
    role: str,
    content: str,
    message_type: str = "text",
) -> None:
    """Save a message; bumps conversation message_count and last_message_at."""
    conversation_id = _get_or_create_conversation(bot_id, customer_phone)
    if not conversation_id:
        return

    client().table("messages").insert(
        {
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "message_type": message_type,
        }
    ).execute()

    try:
        client().rpc(
            "increment_message_count", {"conv_id": conversation_id}
        ).execute()
    except Exception:
        # Non-fatal — count will drift but messages are saved
        pass


def save_booking(
    bot_id: str,
    customer_phone: str,
    booking: dict,
) -> None:
    """Persist a booking record extracted from the AI reply."""
    if not booking or not isinstance(booking, dict):
        return

    date = (booking.get("date") or "").strip() or None
    time = (booking.get("time") or "").strip() or None
    scheduled_at = None
    scheduled_time_text = booking.get("time_text") or None

    if date and time and len(date) == 10 and len(time) >= 4:
        # Combine into UTC-naive ISO. Postgres will treat as timestamptz (UTC).
        scheduled_at = f"{date}T{time}:00"
        scheduled_time_text = scheduled_time_text or f"{date} {time}"

    status = (booking.get("status") or "confirmed").strip().lower()
    if status not in ("pending", "confirmed", "cancelled", "completed", "no_show"):
        status = "confirmed"

    conv_id = _get_or_create_conversation(bot_id, customer_phone)

    payload = {
        "bot_id": bot_id,
        "conversation_id": conv_id,
        "customer_phone": customer_phone,
        "customer_name": booking.get("customer_name"),
        "service": booking.get("service"),
        "scheduled_at": scheduled_at,
        "scheduled_time_text": scheduled_time_text,
        "duration_minutes": booking.get("duration_minutes"),
        "price_azn": booking.get("price_azn"),
        "status": status,
        "notes": booking.get("notes"),
        "raw_payload": booking,
    }
    # Strip Nones — Postgres prefers omitted over null for some columns
    payload = {k: v for k, v in payload.items() if v is not None}

    try:
        client().table("bookings").insert(payload).execute()
        print(f"[booking] saved: {payload.get('service')} @ {payload.get('scheduled_at')}")
    except Exception as e:
        print(f"[booking] save failed: {e}")


def get_recent_history(
    bot_id: str, customer_phone: str, limit: int = 10
) -> list[dict]:
    """Load last N messages for (bot, phone) ordered oldest → newest."""
    try:
        conv = (
            client()
            .table("conversations")
            .select("id")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .maybe_single()
            .execute()
        )
        if not conv or not conv.data:
            return []
        conv_id = conv.data["id"]

        msgs = (
            client()
            .table("messages")
            .select("role, content")
            .eq("conversation_id", conv_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(reversed(msgs.data or []))
    except Exception:
        return []
