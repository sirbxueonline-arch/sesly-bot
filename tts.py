"""
Text-to-speech via ElevenLabs.

We use multilingual_v2 which handles AZ + RU well. Voice can be overridden
per bot (bots.voice_voice_id), otherwise we use ELEVENLABS_DEFAULT_VOICE or
a hardcoded multilingual default.
"""
from __future__ import annotations
import os
import tempfile
from typing import Optional
import requests

ELEVEN_API = "https://api.elevenlabs.io/v1"

# 'Sarah' — clean, multilingual, English-default but solid on AZ
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))


def synthesize(text: str, voice_id: Optional[str] = None) -> Optional[str]:
    """
    Convert text to MP3. Returns local file path or None on failure.
    """
    api_key = os.getenv("ELEVENLABS_API_KEY")
    if not api_key:
        return None
    text = (text or "").strip()
    if not text:
        return None

    voice = (
        voice_id
        or os.getenv("ELEVENLABS_DEFAULT_VOICE")
        or DEFAULT_VOICE_ID
    )
    url = f"{ELEVEN_API}/text-to-speech/{voice}"

    # Cap input — ElevenLabs charges per character, and WhatsApp voice msgs
    # over ~30s feel slow. 1500 chars ≈ 25-30 s spoken.
    payload = {
        "text": text[:1500],
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
        },
    }
    try:
        r = requests.post(
            url,
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=payload,
            timeout=30,
        )
        if r.status_code != 200:
            print(f"[tts] {r.status_code}: {r.text[:300]}")
            return None
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        tmp.write(r.content)
        tmp.flush()
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"[tts] error: {e}")
        return None
