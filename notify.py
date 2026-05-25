"""
Outbound WhatsApp notifications to business owners.

Uses Meta WhatsApp Cloud API. Requires META_ACCESS_TOKEN and
META_PHONE_NUMBER_ID env vars (the latter is the Meta-side ID of the
Sesly business number — found in API Setup page).
"""
from __future__ import annotations
import os
import json
from typing import Optional
import requests

GRAPH_API_VERSION = os.getenv("META_GRAPH_VERSION", "v20.0")


def _normalize_phone(phone: str) -> Optional[str]:
    """Meta wants digits only, no '+'. Return None if invalid."""
    digits = "".join(c for c in (phone or "") if c.isdigit())
    if len(digits) < 8:
        return None
    return digits


def send_to_owner(business_phone: str, body: str) -> bool:
    """Send a free-form WhatsApp text to the business owner. Returns True on 2xx."""
    token = os.getenv("META_ACCESS_TOKEN")
    phone_number_id = os.getenv("META_PHONE_NUMBER_ID")
    if not token or not phone_number_id:
        print("[notify] missing META_ACCESS_TOKEN / META_PHONE_NUMBER_ID")
        return False
    to = _normalize_phone(business_phone)
    if not to:
        print(f"[notify] invalid business phone: {business_phone!r}")
        return False

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=12,
        )
        if r.status_code >= 400:
            print(f"[notify] HTTP {r.status_code}: {r.text}")
            return False
        return True
    except Exception as e:
        print(f"[notify] error: {e}")
        return False
