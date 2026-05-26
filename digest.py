"""
Daily WhatsApp digest for business owners.

Sent by a Vercel cron (08pm Asia/Baku = 16:00 UTC) hitting `/cron/digest`.

For each business that has a phone number configured, we aggregate the last
24 hours of activity (messages, unique customers, bookings) across all of
that business's bots and send a single WhatsApp summary to the owner.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Optional

import db
from notify import send_to_owner


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------

def _yesterday_window_utc() -> tuple[datetime, datetime]:
    """Return (since, until) — last 24 hours up to "now" in UTC."""
    until = datetime.now(timezone.utc)
    since = until - timedelta(hours=24)
    return since, until


def _aggregate_business(business: dict, since: datetime, until: datetime) -> Optional[dict]:
    """Return a stats dict for a single business, or None if no activity."""
    bid = business.get("id")
    if not bid:
        return None

    c = db.client()
    since_iso = since.isoformat()
    until_iso = until.isoformat()

    # Bots belonging to this business
    bots_res = c.table("bots").select("id, handle, display_name").eq("business_id", bid).execute()
    bots = bots_res.data or []
    if not bots:
        return None
    bot_ids = [b["id"] for b in bots]

    # Messages in window (user-role only — that's "incoming")
    msgs_res = (
        c.table("messages")
        .select("id, conversation_id, role, created_at")
        .in_("conversation_id", _conversation_ids_for_bots(c, bot_ids))
        .gte("created_at", since_iso)
        .lt("created_at", until_iso)
        .execute()
    )
    msgs = msgs_res.data or []
    user_msgs = [m for m in msgs if m.get("role") == "user"]
    msg_count = len(user_msgs)
    if msg_count == 0:
        # Skip silent businesses — no point pinging them
        return None

    # Unique customers
    convo_ids = {m["conversation_id"] for m in user_msgs if m.get("conversation_id")}
    customers = set()
    if convo_ids:
        convos_res = (
            c.table("conversations")
            .select("id, customer_phone")
            .in_("id", list(convo_ids))
            .execute()
        )
        for row in convos_res.data or []:
            phone = (row.get("customer_phone") or "").strip()
            if phone:
                customers.add(phone)

    # Bookings in window
    bookings_res = (
        c.table("bookings")
        .select("id, status, bot_id")
        .in_("bot_id", bot_ids)
        .gte("created_at", since_iso)
        .lt("created_at", until_iso)
        .execute()
    )
    bookings = bookings_res.data or []
    confirmed = sum(1 for b in bookings if b.get("status") == "confirmed")
    total_bookings = len(bookings)

    # Top bot by message count
    counts_per_bot: dict[str, int] = {}
    bot_by_convo: dict[str, str] = {}
    if convo_ids:
        convos_b = (
            c.table("conversations")
            .select("id, bot_id")
            .in_("id", list(convo_ids))
            .execute()
        )
        for row in convos_b.data or []:
            bot_by_convo[row["id"]] = row["bot_id"]
    for m in user_msgs:
        bid_ = bot_by_convo.get(m["conversation_id"])
        if bid_:
            counts_per_bot[bid_] = counts_per_bot.get(bid_, 0) + 1
    top_bot_id = max(counts_per_bot, key=counts_per_bot.get) if counts_per_bot else None
    top_bot = next((b for b in bots if b["id"] == top_bot_id), None) if top_bot_id else None

    return {
        "business": business,
        "msg_count": msg_count,
        "customer_count": len(customers),
        "bookings_total": total_bookings,
        "bookings_confirmed": confirmed,
        "top_bot": top_bot,
        "top_bot_msgs": counts_per_bot.get(top_bot_id, 0) if top_bot_id else 0,
    }


def _conversation_ids_for_bots(c, bot_ids: list[str]) -> list[str]:
    if not bot_ids:
        return []
    res = c.table("conversations").select("id").in_("bot_id", bot_ids).execute()
    return [r["id"] for r in (res.data or [])]


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------

def _format_digest(stats: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%d %B")
    lines = [
        f"☀️ Sesly günün özeti — {today}",
        "",
        f"📨 {stats['msg_count']} mesaj",
        f"👥 {stats['customer_count']} müştəri",
    ]
    if stats["bookings_total"] > 0:
        if stats["bookings_confirmed"] == stats["bookings_total"]:
            lines.append(f"✅ {stats['bookings_total']} randevu")
        else:
            lines.append(
                f"✅ {stats['bookings_total']} randevu "
                f"({stats['bookings_confirmed']} təsdiqlənmiş)"
            )
    else:
        lines.append("✅ randevu yoxdur")

    if stats.get("top_bot"):
        lines.append("")
        lines.append(
            f"🤖 Aktiv bot: /{stats['top_bot']['handle']} ({stats['top_bot_msgs']} mesaj)"
        )

    lines.append("")
    lines.append("Sabah yenidən görüşənədək 💛")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def run_daily_digest() -> dict:
    """Iterate over every business with a phone and send a digest. Returns stats."""
    c = db.client()
    since, until = _yesterday_window_utc()

    biz_res = c.table("businesses").select("id, name, phone, user_id").execute()
    businesses = biz_res.data or []

    sent = 0
    skipped = 0
    failed = 0

    for biz in businesses:
        owner_phone = (biz.get("phone") or "").strip()
        if not owner_phone:
            skipped += 1
            continue

        stats = _aggregate_business(biz, since, until)
        if not stats:
            skipped += 1
            continue

        body = _format_digest(stats)
        ok = send_to_owner(owner_phone, body)
        if ok:
            sent += 1
            print(f"[digest] sent to {biz.get('name')!r} at {owner_phone}")
        else:
            failed += 1
            print(f"[digest] FAILED to {biz.get('name')!r}")

    return {
        "businesses_total": len(businesses),
        "sent": sent,
        "skipped_silent_or_no_phone": skipped,
        "failed": failed,
        "window_since": since.isoformat(),
        "window_until": until.isoformat(),
    }
