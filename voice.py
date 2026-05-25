"""
Voice note transcription with OpenAI Whisper.

Twilio sends a MediaUrl to the WhatsApp voice note. We download it
(using Twilio basic auth) and pass to Whisper.
"""
from __future__ import annotations
import os
import tempfile
from typing import Optional
import requests
from openai import OpenAI

_client: Optional[OpenAI] = None


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY must be set")
        _client = OpenAI(api_key=key)
    return _client


def download_twilio_media(media_url: str) -> Optional[str]:
    """Download voice file from Twilio (auth required). Returns local path."""
    sid = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    if not sid or not token:
        return None
    try:
        r = requests.get(media_url, auth=(sid, token), timeout=20)
        r.raise_for_status()
        suffix = ".ogg"
        ct = r.headers.get("Content-Type", "").lower()
        if "mp3" in ct:
            suffix = ".mp3"
        elif "mp4" in ct or "m4a" in ct:
            suffix = ".m4a"
        elif "wav" in ct:
            suffix = ".wav"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.content)
        tmp.flush()
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"[voice] download failed: {e}")
        return None


def transcribe(file_path: str, language: str = "az") -> Optional[str]:
    """Transcribe an audio file with Whisper. Returns text or None."""
    try:
        with open(file_path, "rb") as f:
            resp = client().audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language=language,
            )
        return (resp.text or "").strip() or None
    except Exception as e:
        print(f"[voice] transcribe failed: {e}")
        return None
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


def transcribe_from_url(media_url: str, language: str = "az") -> Optional[str]:
    """Convenience: download Twilio media then transcribe."""
    path = download_twilio_media(media_url)
    if not path:
        return None
    return transcribe(path, language=language)
