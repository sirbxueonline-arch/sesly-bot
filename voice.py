"""
Voice note transcription with OpenAI Whisper, using Meta Cloud API.

Meta sends an audio message with a `media_id`. To download the file:
1. GET https://graph.facebook.com/v20.0/<media-id>  (returns a temporary URL)
2. GET <that URL>  (with Bearer auth) — returns the raw audio bytes
"""
from __future__ import annotations
import os
import tempfile
from typing import Optional
import requests
from openai import OpenAI

_client: Optional[OpenAI] = None

GRAPH_API_VERSION = os.getenv("META_GRAPH_VERSION", "v20.0")
GRAPH_API = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def client() -> OpenAI:
    global _client
    if _client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY must be set")
        _client = OpenAI(api_key=key)
    return _client


def _suffix_for(mime: str) -> str:
    mime = (mime or "").lower()
    if "ogg" in mime or "opus" in mime:
        return ".ogg"
    if "mp4" in mime or "m4a" in mime or "aac" in mime:
        return ".m4a"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    if "wav" in mime:
        return ".wav"
    return ".ogg"  # WhatsApp default


def download_meta_media(media_id: str) -> Optional[str]:
    """Download a Meta-hosted media file. Returns local path or None."""
    token = os.getenv("META_ACCESS_TOKEN")
    if not token:
        print("[voice] missing META_ACCESS_TOKEN")
        return None
    try:
        # Step 1: resolve media_id → temporary download URL
        meta = requests.get(
            f"{GRAPH_API}/{media_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        meta.raise_for_status()
        info = meta.json()
        url = info.get("url")
        mime = info.get("mime_type", "")
        if not url:
            print(f"[voice] no url in media meta: {info}")
            return None

        # Step 2: download the binary
        r = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
        r.raise_for_status()

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=_suffix_for(mime))
        tmp.write(r.content)
        tmp.flush()
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"[voice] download failed: {e}")
        return None


def transcribe(file_path: str, language: Optional[str] = None) -> Optional[str]:
    """Transcribe an audio file with Whisper.

    By default we DON'T force a language — Whisper auto-detects, so a
    Russian or English voice message is transcribed correctly instead
    of being forced through the Azerbaijani phoneme model. Pass
    language='az' explicitly only if you want to bias toward AZ."""
    try:
        with open(file_path, "rb") as f:
            kwargs: dict = {"model": "whisper-1", "file": f}
            if language:
                kwargs["language"] = language
            resp = client().audio.transcriptions.create(**kwargs)
        return (resp.text or "").strip() or None
    except Exception as e:
        print(f"[voice] transcribe failed: {e}")
        return None
    finally:
        try:
            os.remove(file_path)
        except OSError:
            pass


def transcribe_meta_media(media_id: str, language: Optional[str] = None) -> Optional[str]:
    """Convenience: download Meta media then transcribe.
    Whisper auto-detects language by default; pass language='az' to bias."""
    path = download_meta_media(media_id)
    if not path:
        return None
    return transcribe(path, language=language)
