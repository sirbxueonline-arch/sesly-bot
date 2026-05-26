"""
Text-to-speech via ElevenLabs, then re-encoded to OGG/Opus so that
WhatsApp renders the reply as an actual voice note (push-to-talk
waveform) instead of a generic audio attachment.

Pipeline:
  ElevenLabs → MP3 → ffmpeg → OGG/Opus mono 16 kHz

We use multilingual_v2 which handles AZ + RU well. Voice can be overridden
per bot (bots.voice_voice_id), otherwise we use ELEVENLABS_DEFAULT_VOICE or
a hardcoded multilingual default.
"""
from __future__ import annotations
import os
import subprocess
import tempfile
from typing import Optional
import requests

ELEVEN_API = "https://api.elevenlabs.io/v1"

# 'Sarah' — clean, multilingual, English-default but solid on AZ
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"


def is_configured() -> bool:
    return bool(os.getenv("ELEVENLABS_API_KEY"))


def _ffmpeg_path() -> Optional[str]:
    """Locate a usable ffmpeg binary. Prefers the imageio-ffmpeg bundle so
    the deployment doesn't depend on the system having ffmpeg installed."""
    try:
        import imageio_ffmpeg  # type: ignore
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        # Fall back to PATH (rare on Vercel, helps in local dev)
        from shutil import which
        return which("ffmpeg")


def _mp3_to_ogg_opus(mp3_path: str) -> Optional[str]:
    """Convert mp3 → ogg/opus (mono, 16 kHz, 24 kbps) — what WhatsApp
    expects for voice-note rendering. Returns the new .ogg path or None
    if conversion fails (caller should fall back to sending mp3)."""
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        print("[tts] ffmpeg not available — falling back to MP3")
        return None

    out_path = tempfile.NamedTemporaryFile(delete=False, suffix=".ogg").name
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-y",                       # overwrite
                "-i", mp3_path,
                "-vn",                      # no video stream
                "-ac", "1",                 # mono
                "-ar", "16000",             # 16 kHz
                "-c:a", "libopus",
                "-b:a", "24k",
                "-f", "ogg",
                out_path,
            ],
            capture_output=True,
            timeout=20,
        )
        if proc.returncode != 0:
            print(f"[tts] ffmpeg failed rc={proc.returncode}: {proc.stderr.decode('utf-8', 'ignore')[:300]}")
            try: os.unlink(out_path)
            except Exception: pass
            return None
        return out_path
    except subprocess.TimeoutExpired:
        print("[tts] ffmpeg timed out")
        try: os.unlink(out_path)
        except Exception: pass
        return None
    except Exception as e:
        print(f"[tts] ffmpeg threw: {e}")
        try: os.unlink(out_path)
        except Exception: pass
        return None


def synthesize(text: str, voice_id: Optional[str] = None) -> Optional[str]:
    """
    Convert text to a WhatsApp-ready audio file. Returns a local file path
    pointing at an OGG/Opus file (preferred) or an MP3 (fallback) when
    ffmpeg conversion fails. Returns None if TTS itself failed.
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
    mp3_path: Optional[str] = None
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
        mp3_path = tmp.name
    except Exception as e:
        print(f"[tts] error: {e}")
        return None

    # Now convert mp3 → ogg/opus
    ogg_path = _mp3_to_ogg_opus(mp3_path)
    if ogg_path:
        # Drop the intermediate mp3
        try: os.unlink(mp3_path)
        except Exception: pass
        return ogg_path

    # Couldn't convert — caller will still send the mp3 (as audio attachment).
    return mp3_path
