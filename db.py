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


def get_telegram_contact(chat_id: str | int) -> Optional[dict]:
    """Return the saved contact record for a Telegram chat_id, or None.
    chat_id is the bare digits (no 'tg:' prefix)."""
    if chat_id is None:
        return None
    try:
        r = (
            client()
            .table("telegram_contacts")
            .select("*")
            .eq("chat_id", str(chat_id))
            .maybe_single()
            .execute()
        )
        return r.data if r else None
    except Exception as e:
        print(f"[db] get_telegram_contact failed: {e}")
        return None


def has_telegram_contact(chat_id: str | int) -> bool:
    """Cheap check: do we already have a phone for this chat_id?"""
    row = get_telegram_contact(chat_id)
    return bool(row and row.get("phone"))


def save_telegram_contact(
    chat_id: str | int,
    *,
    phone: Optional[str],
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    username: Optional[str] = None,
    language_code: Optional[str] = None,
    awaiting_surname: Optional[bool] = None,
) -> None:
    """Upsert a Telegram contact row. Called when a customer taps the
    request_contact button and Telegram sends us their phone, and again
    later if we collect their surname via follow-up."""
    if not chat_id:
        return
    payload = {
        "chat_id": str(chat_id),
        "phone": phone,
        "first_name": first_name,
        "last_name": last_name,
        "username": username,
        "language_code": language_code,
        "awaiting_surname": awaiting_surname,
        "updated_at": _utcnow_iso(),
    }
    # Drop Nones so we don't overwrite existing values with NULL on later
    # interactions (e.g. when we only have updated profile but not phone)
    payload = {k: v for k, v in payload.items() if v is not None}
    try:
        client().table("telegram_contacts").upsert(
            payload, on_conflict="chat_id"
        ).execute()
    except Exception as e:
        print(f"[db] save_telegram_contact failed: {e}")


def set_telegram_awaiting_surname(chat_id: str | int, value: bool) -> None:
    """Flip the awaiting_surname flag for a chat without touching other
    fields. Used to mark "next message is the surname" and to clear it
    after we've saved the response."""
    if not chat_id:
        return
    try:
        client().table("telegram_contacts").update({
            "awaiting_surname": value,
            "updated_at": _utcnow_iso(),
        }).eq("chat_id", str(chat_id)).execute()
    except Exception as e:
        print(f"[db] set_telegram_awaiting_surname failed: {e}")


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def telegram_update_seen(update_id: int) -> bool:
    """Idempotency guard. Returns True if this Telegram update_id has
    already been processed (caller should skip), False if new (caller
    should process it).

    Telegram retries webhooks if our response is slow. Without this guard
    a single message can get two replies. Backed by the
    telegram_processed_updates table (migration 016).
    """
    if not update_id:
        return False
    try:
        client().table("telegram_processed_updates").insert(
            {"update_id": int(update_id)}
        ).execute()
        return False
    except Exception as e:
        s = str(e).lower()
        # Postgres duplicate key violation OR PostgREST conflict response
        if (
            "duplicate" in s
            or "23505" in s
            or "already exists" in s
            or "conflict" in s
        ):
            return True
        # Any other error — log and let the message through (we'd rather
        # double-reply than drop a message entirely)
        print(f"[tg-dedup] insert error (allowing through): {e}")
        return False


def get_bot_staff(bot_id: str) -> list[dict]:
    """Active staff members for a bot, ordered as the owner arranged them."""
    if not bot_id:
        return []
    try:
        r = (
            client()
            .table("bot_staff")
            .select("id, name, role, bio, emoji")
            .eq("bot_id", bot_id)
            .eq("is_active", True)
            .order("sort_order")
            .execute()
        )
        return r.data or []
    except Exception as e:
        print(f"[db] get_bot_staff failed: {e}")
        return []


def get_customer_history(bot_id: str, customer_phone: str) -> dict:
    """
    Return a small summary of this customer's history with the bot. Used
    to make the AI greet returning customers naturally.

    {
      "is_returning": bool,
      "total_visits": int,
      "last_visit_at": iso str or None,
      "last_service": str or None,
      "no_shows": int,
      "name": str or None,
    }
    """
    if not bot_id or not customer_phone:
        return {"is_returning": False}
    try:
        # First: try the summary view
        view = (
            client()
            .table("v_customer_last_visit")
            .select("*")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .maybe_single()
            .execute()
        )
        if not view or not view.data:
            return {"is_returning": False}
        info = view.data

        # Also fetch the most recent booking row for the name + last service
        recent = (
            client()
            .table("bookings")
            .select("customer_name, service, status, scheduled_at")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        recent_row = (recent.data or [None])[0] or {}

        return {
            "is_returning": (info.get("total_visits") or 0) >= 1,
            "total_visits": info.get("total_visits") or 0,
            "completed_visits": info.get("completed_visits") or 0,
            "last_visit_at": info.get("last_visit_at"),
            "last_service": recent_row.get("service"),
            "no_shows": info.get("no_show_count") or 0,
            "name": recent_row.get("customer_name"),
        }
    except Exception as e:
        print(f"[db] get_customer_history failed: {e}")
        return {"is_returning": False}


def get_bot_by_id(bot_id: str) -> Optional[dict]:
    """Look up bot config by id. Used by the dashboard preview endpoint."""
    bid = (bot_id or "").strip()
    if not bid:
        return None
    try:
        result = (
            client()
            .table("bots")
            .select("*, businesses(name, type, plan, user_id)")
            .eq("id", bid)
            .maybe_single()
            .execute()
        )
        if result and result.data:
            return result.data
    except Exception as e:
        print(f"[db] get_bot_by_id failed: {e}")
    return None


# Plan limits — mirrors lib/plans.ts in the dashboard.
# Plan rename May 2026: start→pro, pro→max. We keep the old keys as
# aliases so this code still works on databases that haven't applied
# migration 018 yet.
PLAN_LIMITS = {
    "free":  {"messages": 100,  "bots": 1, "name": "Sınaq"},
    "pro":   {"messages": 1000, "bots": 3, "name": "Pro"},
    "max":   {"messages": None, "bots": 5, "name": "Max"},
    # Legacy aliases:
    "start": {"messages": 1000, "bots": 3, "name": "Pro"},  # old Başlanğıc → Pro
}


def get_monthly_user_message_count(business_id: str) -> int:
    """
    Return the count of customer (user-role) messages in the current calendar
    month for the business.

    Reads from `usage_ledger` (migration 010) which is incremented by a DB
    trigger on every messages insert. This count persists even if the user
    deletes a bot — so it can't be reset to bypass the monthly plan cap.
    """
    if not business_id:
        return 0
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        month_key = now.strftime("%Y-%m")

        result = (
            client()
            .table("usage_ledger")
            .select("message_count")
            .eq("business_id", business_id)
            .eq("month_key", month_key)
            .maybe_single()
            .execute()
        )
        if result and result.data:
            return int(result.data.get("message_count") or 0)
        return 0
    except Exception as e:
        print(f"[db] usage_ledger lookup failed: {e}")
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


def _parse_scheduled(value):
    """
    Parse a scheduled_at value into a UTC-naive datetime, truncated to the
    minute. Returns None if it can't be parsed. Handles all of:
      - "2026-05-26T14:00:00"          (AI emits this)
      - "2026-05-26T14:00:00+00:00"   (Postgres returns this)
      - "2026-05-26T14:00:00Z"
      - datetime objects
    """
    if not value:
        return None
    try:
        from datetime import datetime
        if hasattr(value, "isoformat"):
            dt = value
        else:
            s = str(value).strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt.replace(second=0, microsecond=0)
    except Exception:
        return None


def _should_merge_booking(old: dict, new_payload: dict, now_utc):
    """
    Decide whether the new booking should UPDATE the existing one.
    Returns (bool, reason).
    """
    old_at = _parse_scheduled(old.get("scheduled_at"))
    new_at = _parse_scheduled(new_payload.get("scheduled_at"))

    # 1) Same parsed slot → same appointment, definitely merge
    if old_at and new_at and old_at == new_at:
        return True, "same scheduled_at slot"

    # 2) Same service name (case-insensitive) → same appointment
    old_svc = (old.get("service") or "").strip().lower()
    new_svc = (new_payload.get("service") or "").strip().lower()
    if old_svc and new_svc and old_svc == new_svc:
        return True, "same service"

    # 3) Old was pending and new is confirmed within 30 min → status upgrade
    try:
        from datetime import datetime, timezone
        old_created = old.get("created_at")
        if old_created:
            s = old_created
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            old_created_dt = datetime.fromisoformat(s)
            if old_created_dt.tzinfo is None:
                old_created_dt = old_created_dt.replace(tzinfo=timezone.utc)
            age_min = (now_utc - old_created_dt).total_seconds() / 60
            if (
                old.get("status") == "pending"
                and new_payload.get("status") == "confirmed"
                and age_min < 30
            ):
                return True, f"pending → confirmed within {int(age_min)} min"
    except Exception:
        pass

    # 4) Both lack scheduled_at AND created within last 10 min → same convo
    if not old_at and not new_at:
        try:
            from datetime import datetime, timezone
            old_created = old.get("created_at")
            if old_created:
                s = old_created
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                old_created_dt = datetime.fromisoformat(s)
                if old_created_dt.tzinfo is None:
                    old_created_dt = old_created_dt.replace(tzinfo=timezone.utc)
                age_min = (now_utc - old_created_dt).total_seconds() / 60
                if age_min < 10:
                    return True, "no slot, same recent conversation"
        except Exception:
            pass

    return False, "different appointment"


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
        # The AI prompt anchors everything to Bakı vaxtı (UTC+4), so the time
        # we receive is Baku-local. Stamp the offset explicitly so Postgres
        # stores the correct UTC instant and downstream consumers (Google
        # Calendar, dashboard) display the right wall-clock time.
        scheduled_at = f"{date}T{time}:00+04:00"
        scheduled_time_text = scheduled_time_text or f"{date} {time}"

    status = (booking.get("status") or "confirmed").strip().lower()
    if status not in ("pending", "confirmed", "cancelled", "completed", "no_show"):
        status = "confirmed"

    conv_id = _get_or_create_conversation(bot_id, customer_phone)

    # Resolve staff_id from staff_name (if AI provided one)
    staff_name = (booking.get("staff_name") or "").strip() or None
    staff_id = None
    if staff_name:
        try:
            sres = (
                client()
                .table("bot_staff")
                .select("id, name")
                .eq("bot_id", bot_id)
                .eq("is_active", True)
                .ilike("name", f"%{staff_name}%")
                .limit(1)
                .execute()
            )
            row = (sres.data or [None])[0]
            if row:
                staff_id = row["id"]
                staff_name = row["name"]  # snap to canonical name
        except Exception as e:
            print(f"[bookings] staff lookup failed for {staff_name!r}: {e}")

    # Enrich customer_name with the Telegram contact's first+last name
    # if the AI didn't already collect one. This means a customer who
    # books via Telegram after sharing their contact gets "Aysel Mammadova"
    # on the booking instead of an empty name field.
    customer_name = booking.get("customer_name")
    if not customer_name and customer_phone.startswith("tg:"):
        try:
            tg_chat_id = customer_phone[3:]
            contact = get_telegram_contact(tg_chat_id)
            if contact:
                parts = [contact.get("first_name"), contact.get("last_name")]
                joined = " ".join([p for p in parts if p]).strip()
                if joined:
                    customer_name = joined
        except Exception as e:
            print(f"[bookings] tg contact name lookup failed: {e}")

    payload = {
        "bot_id": bot_id,
        "conversation_id": conv_id,
        "customer_phone": customer_phone,
        "customer_name": customer_name,
        "service": booking.get("service"),
        "scheduled_at": scheduled_at,
        "scheduled_time_text": scheduled_time_text,
        "duration_minutes": booking.get("duration_minutes"),
        "price_azn": booking.get("price_azn"),
        "status": status,
        "notes": booking.get("notes"),
        "staff_id": staff_id,
        "staff_name_at_booking": staff_name,
        "raw_payload": booking,
    }
    # Strip Nones — Postgres prefers omitted over null for some columns
    payload = {k: v for k, v in payload.items() if v is not None}

    # Dedup: find the most recent booking for the SAME (bot, phone) in the last
    # 4 hours. If it's clearly the same appointment (matching slot, OR same
    # service, OR recent pending row from the same conversation), UPDATE
    # instead of INSERT.
    try:
        from datetime import datetime, timezone, timedelta

        now_utc = datetime.now(timezone.utc)
        window_start = (now_utc - timedelta(hours=4)).isoformat()

        existing = (
            client()
            .table("bookings")
            .select("id, scheduled_at, status, service, created_at")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .gte("created_at", window_start)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if existing.data:
            old = existing.data[0]
            should_update, reason = _should_merge_booking(old, payload, now_utc)
            print(
                f"[booking] dedup check: old_at={old.get('scheduled_at')!r} "
                f"new_at={payload.get('scheduled_at')!r} "
                f"old_status={old.get('status')} new_status={payload.get('status')} "
                f"→ merge={should_update} ({reason})"
            )
            if should_update:
                update_payload = {
                    k: v for k, v in payload.items() if k not in ("conversation_id",)
                }
                client().table("bookings").update(update_payload).eq("id", old["id"]).execute()
                print(f"[booking] updated existing {old['id'][:8]}… ({reason})")
                # Notify the owner only when transitioning INTO confirmed
                if (
                    old.get("status") != "confirmed"
                    and payload.get("status") == "confirmed"
                ):
                    _notify_owner_of_booking(bot_id, customer_phone, payload)
                return
    except Exception as e:
        print(f"[booking] dedup check failed: {e}")

    # No match — insert new
    inserted = False
    try:
        client().table("bookings").insert(payload).execute()
        inserted = True
        print(f"[booking] inserted: {payload.get('service')} @ {payload.get('scheduled_at')}")
    except Exception as e:
        print(f"[booking] insert failed: {e}")

    if inserted and status == "confirmed":
        _notify_owner_of_booking(bot_id, customer_phone, payload)


def detect_cancellation_intent(message: str) -> bool:
    """Return True if the customer message looks like 'cancel my booking'."""
    if not message:
        return False
    text = message.strip().lower()
    if len(text) > 80:
        return False
    triggers = [
        "ləğv et", "ləğv edirəm", "ləğv olun",
        "ləğv etmək istəyirəm", "ləğv olunsun",
        "iptal", "iptal et",       # TR-leaning but real users say this
        "cancel", "cancel my appointment",
        "imtina edirəm", "gəlməyəcəm", "gələ bilməyəcəm",
        "randevunu ləğv", "randevumu ləğv",
    ]
    return any(t in text for t in triggers)


def cancel_latest_booking(bot_id: str, customer_phone: str) -> Optional[dict]:
    """
    Cancel the most recent active booking for this customer.
    Returns the cancelled booking row or None if nothing to cancel.
    """
    if not bot_id or not customer_phone:
        return None
    try:
        recent = (
            client()
            .table("bookings")
            .select("id, service, scheduled_at, scheduled_time_text, status")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .in_("status", ["pending", "confirmed"])
            .order("scheduled_at", desc=True)
            .limit(1)
            .execute()
        )
        row = (recent.data or [None])[0]
        if not row:
            return None
        client().table("bookings").update({"status": "cancelled"}).eq("id", row["id"]).execute()
        print(f"[bookings] cancelled {row['id']} for {customer_phone}")
        row["status"] = "cancelled"
        return row
    except Exception as e:
        print(f"[bookings] cancel failed: {e}")
        return None


def check_slot_conflict(
    bot_id: str,
    scheduled_at: Optional[str],
    duration_minutes: Optional[int],
    exclude_booking_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Return a conflicting booking if the proposed slot overlaps with another
    confirmed/pending booking. Returns None if no conflict.
    Conflict window = [scheduled_at, scheduled_at + duration_minutes].
    """
    if not bot_id or not scheduled_at:
        return None
    try:
        from datetime import datetime, timedelta
        normalized = scheduled_at.replace("Z", "+00:00") if "Z" in scheduled_at else scheduled_at
        start = datetime.fromisoformat(normalized)
        end = start + timedelta(minutes=duration_minutes or 60)
        # Pull bookings on the same day, filter overlap in Python (small set)
        day_start = start.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        day_end = (start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).isoformat()
        q = (
            client()
            .table("bookings")
            .select("id, service, scheduled_at, duration_minutes, status, customer_name, customer_phone")
            .eq("bot_id", bot_id)
            .in_("status", ["pending", "confirmed"])
            .gte("scheduled_at", day_start)
            .lt("scheduled_at", day_end)
            .execute()
        )
        for b in (q.data or []):
            if exclude_booking_id and b["id"] == exclude_booking_id:
                continue
            other_start = datetime.fromisoformat(
                b["scheduled_at"].replace("Z", "+00:00") if "Z" in (b.get("scheduled_at") or "") else b["scheduled_at"]
            )
            other_end = other_start + timedelta(minutes=b.get("duration_minutes") or 60)
            # Overlap if start < other_end AND end > other_start
            if start < other_end and end > other_start:
                return b
        return None
    except Exception as e:
        print(f"[bookings] conflict check failed: {e}")
        return None


def detect_and_save_review(
    bot_id: str, customer_phone: str, message: str
) -> Optional[int]:
    """
    If `message` looks like a 1-5 star rating, find this customer's most
    recent confirmed booking with this bot in the last 14 days and save
    a review row. Returns the rating (1-5) on success, None otherwise.
    """
    import re
    from datetime import datetime, timezone, timedelta

    if not message:
        return None
    # Accept "5", "5 ulduz", "5/5", "5 star", "⭐⭐⭐⭐⭐", but ONLY if the
    # message is essentially just the rating (under 30 chars, contains a
    # 1-5 digit at the start OR just stars).
    text = message.strip()
    if len(text) > 30:
        return None

    # Star count via emoji
    star_count = text.count("⭐") + text.count("★")
    if star_count >= 1 and star_count <= 5:
        rating = star_count
    else:
        m = re.match(r"^([1-5])(?!\d)", text)
        if not m:
            return None
        rating = int(m.group(1))

    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        recent = (
            client()
            .table("bookings")
            .select("id, status, created_at")
            .eq("bot_id", bot_id)
            .eq("customer_phone", customer_phone)
            .eq("status", "confirmed")
            .gte("created_at", cutoff)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        booking_row = (recent.data or [None])[0]
        booking_id = booking_row.get("id") if booking_row else None

        # Skip duplicate review for the same booking
        if booking_id:
            existing = (
                client()
                .table("bot_reviews")
                .select("id")
                .eq("bot_id", bot_id)
                .eq("customer_phone", customer_phone)
                .eq("booking_id", booking_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                return None

        client().table("bot_reviews").insert({
            "bot_id": bot_id,
            "customer_phone": customer_phone,
            "booking_id": booking_id,
            "rating": rating,
        }).execute()
        print(f"[reviews] saved {rating}★ for bot={bot_id} customer={customer_phone}")
        return rating
    except Exception as e:
        print(f"[reviews] save failed: {e}")
        return None


def _notify_owner_of_booking(bot_id: str, customer_phone: str, payload: dict) -> None:
    """Send a WhatsApp ping to the business owner that a booking landed,
    and (if Google Calendar is connected for this bot) insert a calendar event."""
    try:
        from notify import send_to_owner
        bot_row = (
            client()
            .table("bots")
            .select(
                "id, display_name, "
                "gcal_refresh_token, gcal_calendar_id, "
                "businesses(phone, name)"
            )
            .eq("id", bot_id)
            .maybe_single()
            .execute()
        )
        if not bot_row or not bot_row.data:
            return
        bot_data = bot_row.data
        biz = bot_data.get("businesses") or {}
        owner_phone = (biz.get("phone") or "").strip()

        biz_name = biz.get("name") or bot_data.get("display_name") or "Botunuz"
        service = payload.get("service") or "Sifariş"
        when = payload.get("scheduled_at")
        when_display = payload.get("scheduled_time_text")
        if when and not when_display:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
                when_display = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                when_display = when

        # Check for conflicting bookings in the same time slot
        conflict = check_slot_conflict(
            bot_id,
            payload.get("scheduled_at"),
            payload.get("duration_minutes"),
            exclude_booking_id=payload.get("id"),
        )

        # WhatsApp owner ping
        if owner_phone:
            lines = ["🔔 Yeni randevu", ""]
            if payload.get("customer_name"):
                lines.append(f"👤 {payload['customer_name']} · {customer_phone}")
            else:
                lines.append(f"👤 {customer_phone}")
            lines.append(f"🛠  {service}")
            if when_display:
                lines.append(f"📅 {when_display}")
            if payload.get("duration_minutes"):
                lines.append(f"⏱  {payload['duration_minutes']} dəq")
            if payload.get("price_azn") is not None:
                lines.append(f"💰 {payload['price_azn']} AZN")
            if payload.get("staff_name_at_booking"):
                lines.append(f"💇 {payload['staff_name_at_booking']}")
            if payload.get("notes"):
                lines.append(f"📝 {payload['notes']}")
            if conflict:
                other_when = conflict.get("scheduled_at") or ""
                other_svc = conflict.get("service") or "başqa randevu"
                other_cust = conflict.get("customer_name") or conflict.get("customer_phone") or "müştəri"
                lines.append("")
                lines.append("⚠️ DİQQƏT — eyni vaxt aralığında başqa randevu var:")
                lines.append(f"   • {other_svc} · {other_cust}")
                lines.append("   Dashboard-da yoxlayın.")
            lines.append("")
            lines.append(f"({biz_name})")

            ok = send_to_owner(owner_phone, "\n".join(lines))
            if ok:
                print(f"[notify] owner alerted at {owner_phone}{' (CONFLICT)' if conflict else ''}")
        else:
            print("[notify] business has no owner phone configured — skipping WA")

        # Google Calendar event (no-op if not connected)
        try:
            from gcal import create_event_for_booking
            create_event_for_booking(bot_data, payload, customer_phone)
        except Exception as e:
            print(f"[gcal] hook failed: {e}")
    except Exception as e:
        print(f"[notify] failed: {e}")


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
