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
            .select("*, businesses(name, type)")
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
    # (helps us see whether the bot exists at all)
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
            # Only return if active
            if result.data.get("is_active"):
                return result.data
    except Exception as e:
        print(f"[db] get_bot_by_handle (fallback) failed: {e}")

    print(f"[db] no active bot found for handle={h!r}")
    return None


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
