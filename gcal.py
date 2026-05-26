"""
Google Calendar integration — write events on the owner's calendar when a
booking is confirmed.

The dashboard handles the OAuth flow and persists:
  - bots.gcal_refresh_token  — long-lived refresh token from Google
  - bots.gcal_calendar_id    — the calendar the owner picked

Here we only need to:
  1. Exchange the refresh token for a short-lived access token
  2. POST a calendar event via Google Calendar API v3

If either env var or DB column is missing we no-op silently.
"""
from __future__ import annotations
import os
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import requests

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal_id}/events"

# AZ is UTC+4 year-round (no DST).
BAKU_TZ = "Asia/Baku"


def _get_access_token(refresh_token: str) -> Optional[str]:
    client_id = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret or not refresh_token:
        return None
    try:
        r = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[gcal] token refresh failed: {r.status_code} {r.text[:300]}")
            return None
        return r.json().get("access_token")
    except Exception as e:
        print(f"[gcal] token refresh error: {e}")
        return None


def _to_baku_iso(dt: datetime) -> str:
    """
    Convert an arbitrary datetime to ISO 8601 in Asia/Baku (UTC+4).
    If `dt` is naive we treat it as UTC (consistent with db._parse_scheduled).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    baku = timezone(timedelta(hours=4))
    return dt.astimezone(baku).isoformat()


def create_event_for_booking(
    bot: dict, payload: dict, customer_phone: str
) -> Optional[str]:
    """
    Insert a Google Calendar event for a confirmed booking.

    Returns the new event id on success, None otherwise (and on no-op).
    """
    refresh_token = (bot.get("gcal_refresh_token") or "").strip()
    calendar_id = (bot.get("gcal_calendar_id") or "").strip()
    if not refresh_token or not calendar_id:
        return None

    scheduled_at = payload.get("scheduled_at")
    if not scheduled_at:
        # If we don't have an actual datetime, skip — owner still gets the
        # WhatsApp notification with the free-text time, that's enough.
        return None

    # Parse scheduled_at (DB stores ISO; may or may not include tz)
    try:
        normalized = scheduled_at.replace("Z", "+00:00")
        start_dt = datetime.fromisoformat(normalized)
    except Exception as e:
        print(f"[gcal] could not parse scheduled_at {scheduled_at!r}: {e}")
        return None

    duration_min = payload.get("duration_minutes") or 60
    try:
        duration_min = int(duration_min)
    except Exception:
        duration_min = 60
    end_dt = start_dt + timedelta(minutes=duration_min)

    access_token = _get_access_token(refresh_token)
    if not access_token:
        return None

    title = payload.get("service") or "Sesly Randevu"
    customer_name = payload.get("customer_name") or customer_phone

    desc_lines = [
        f"👤 Müştəri: {customer_name}",
        f"📞 Telefon: {customer_phone}",
    ]
    if payload.get("price_azn") is not None:
        desc_lines.append(f"💰 Qiymət: {payload['price_azn']} AZN")
    if payload.get("notes"):
        desc_lines.append(f"📝 Qeyd: {payload['notes']}")
    desc_lines.append("")
    desc_lines.append("Sesly tərəfindən avtomatik yaradılıb 💛")

    event = {
        "summary": title,
        "description": "\n".join(desc_lines),
        "start": {"dateTime": _to_baku_iso(start_dt), "timeZone": BAKU_TZ},
        "end":   {"dateTime": _to_baku_iso(end_dt),   "timeZone": BAKU_TZ},
        # Embed metadata so future updates can find the right event
        "extendedProperties": {
            "private": {
                "sesly_booking_id": str(payload.get("id") or ""),
                "sesly_bot_id": str(bot.get("id") or ""),
                "sesly_customer_phone": customer_phone or "",
            }
        },
    }

    url = GOOGLE_EVENTS_URL.format(cal_id=quote(calendar_id, safe=""))
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(event),
            timeout=10,
        )
        if r.status_code in (200, 201):
            event_id = r.json().get("id")
            print(
                f"[gcal] event {event_id} created on calendar={calendar_id!r} "
                f"for bot={bot.get('id')!r}"
            )
            return event_id
        print(f"[gcal] insert failed {r.status_code}: {r.text[:300]}")
        return None
    except Exception as e:
        print(f"[gcal] insert error: {e}")
        return None
